"""
browser_relay.py — Controls your existing Chrome via CDP.

Setup (one time):
  1. Right-click your Chrome shortcut → Properties
  2. Append  --remote-debugging-port=9222  to the Target field
  3. Open Chrome, log into claude.ai / chatgpt.com / etc.
  4. Click "Connect to Chrome" in Relay.

No separate browser windows. No profiles. No anti-bot fights.
"""
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

CDP_URL = "http://localhost:9222"

# Happy-path debug screenshots are a full-page CDP capture (~300-700ms each) and
# fire 3x per send -- the single biggest avoidable latency on the push path. Off
# by default; set RELAY_DEBUG=1 to capture them. Failure screenshots ignore this
# flag (they pass always=True) so a broken send is always documented.
DEBUG_ENABLED = os.environ.get("RELAY_DEBUG", "").strip().lower() not in ("", "0", "false", "no")

SITES: dict[str, dict] = {
    "claude": {
        "url":          "https://claude.ai/new",
        "url_match":    "claude.ai",
        "input":        [
            '[data-testid="chat-input"]',
            'div[contenteditable="true"].ProseMirror',
            ".ProseMirror",
            'div[contenteditable="true"]',
        ],
        "stop_btn":     '[aria-label="Stop"]',
        "send_btn":     '[data-testid="send-button"]',
        "response_sel": [
            # Most specific first — target the prose body inside the assistant turn
            '[data-testid="assistant-message"] .prose-sm',
            '[data-testid="assistant-message"] .prose',
            '[data-testid="assistant-message"] p',
            ".font-claude-message .prose",
            ".font-claude-message",
            '[class*="claude-message"] .prose',
            '[data-is-streaming="false"] .prose',
        ],
    },
    "chatgpt": {
        "url":          "https://chatgpt.com/",
        "url_match":    "chatgpt.com",
        "input":        [
            "#prompt-textarea",
            'div[contenteditable="true"][data-virtualkeyboard]',
        ],
        "stop_btn":     'button[data-testid="stop-button"]',
        "send_btn":     'button[data-testid="send-button"]',
        "response_sel": '[data-message-author-role="assistant"]',
    },
    "gemini": {
        "url":          "https://gemini.google.com/",
        "url_match":    "gemini.google.com",
        "input":        [
            'rich-textarea div[contenteditable="true"]',
            'div[contenteditable="true"][aria-label]',
        ],
        "stop_btn":     'button[aria-label="Stop response"]',
        "send_btn":     'button[aria-label="Send message"]',
        # Gemini's model-response/message-content are Angular custom elements
        # that may use Shadow DOM. Playwright's css engine pierces OPEN shadow
        # roots automatically already (no special ">>"" syntax needed for
        # that case); if these still return empty, the shadow root is CLOSED
        # and no selector syntax can reach inside it -- that needs verifying
        # against the live DOM (inspect model-response, check shadowRoot.mode)
        # before any selector-based fix would actually help.
        "response_sel": [
            "model-response .markdown",
            "message-content .markdown",
            ".response-content",
        ],
    },
    "perplexity": {
        "url":          "https://www.perplexity.ai/",
        "url_match":    "perplexity.ai",
        "input":        [
            # Lexical editor — target by id first, then by attributes
            '#ask-input',
            'div[data-lexical-editor="true"]',
            'div[contenteditable="true"][aria-placeholder]',
            'textarea[placeholder*="Ask"]',
            'div[contenteditable="true"]',
        ],
        "stop_btn":     'button[aria-label="Stop"]',
        "send_btn":     'button[aria-label="Submit"]',
        "response_sel": [
            ".prose",
            "[class*='answer-content']",
            "[class*='response-content']",
            ".answer-text",
            "[class*='answer']",
        ],
    },
    "grok": {
        "url":          "https://grok.com/",
        "url_match":    "grok.com",
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".message-content", "[class*='response']"],
    },
    "copilot": {
        "url":          "https://copilot.microsoft.com/",
        "url_match":    "copilot.microsoft.com",
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".response-message", "[class*='response']"],
    },
}


def site_for_agent(agent_name: str) -> Optional[dict]:
    key = agent_name.lower().replace("_", "").replace(" ", "").replace("-", "")
    for site_key, cfg in SITES.items():
        if site_key in key:
            return {"key": site_key, **cfg}
    return None


# ── Page helpers ────────────────────────────────────────────────────────────────

def _resolve_selector(page, selectors, timeout: int = 1500, prefer_latest: bool = True):
    """Resolve to a single, VISIBLE matching element.

    Hidden/decoy duplicates (legacy SEO textareas, off-screen mobile
    variants, etc.) are skipped -- matching a hidden element and typing
    into it is indistinguishable from doing nothing, which is exactly
    what broke Perplexity's input. Returns the element itself (already
    resolved), not a multi-match Locator -- callers no longer need `.last`.

    timeout defaults to 1500ms (was 8000ms): with 4-5 fallback selectors
    per site, a genuinely missing element used to mean up to ~32-40s before
    giving up -- the first match usually appears fast if it's going to
    appear at all. prefer_latest controls scan direction (latest-first vs
    first-first) for sites where the wrong-but-matching element sits at the
    other end of the DOM order; default keeps existing behavior.
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            loc = page.locator(sel)
            n = loc.count()
            ordering = range(n - 1, -1, -1) if prefer_latest else range(n)
            for i in ordering:
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _force_input_events(page) -> None:
    """Dispatch a real InputEvent + change on the focused element.

    Clipboard paste populates the visible DOM but framework-controlled
    editors (ProseMirror, Lexical, Gemini's rich-textarea) derive the send
    button's enabled state from their internal document model, which only
    updates on a real input/beforeinput transaction -- a raw Ctrl+V doesn't
    always fire one. Without this, the send button stays disabled, Tier 1
    is skipped, and the JS fallback clicks whatever other button happens to
    be enabled (attach/mic/tools) instead -- the root cause of "typed but
    never sent."
    """
    try:
        page.evaluate("""() => {
            const el = document.activeElement;
            if (!el) return;
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertFromPaste',
                data: el.innerText || el.value || '',
            }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
    except Exception:
        pass


def _clipboard_insert(page, message: str) -> bool:
    """Write message to OS clipboard then paste into the focused element.

    execCommand('insertText') is deprecated in recent Chrome and silently fails
    for ProseMirror editors. Clipboard paste works universally: the whole text
    lands in one event with no per-character key events and no newline-as-Enter
    risk.  Returns True on success.
    """
    try:
        # Grant clipboard-write permission for this origin — harmless if already set.
        try:
            page.context.grant_permissions(["clipboard-write"])
        except Exception:
            pass
        page.evaluate("async t => { await navigator.clipboard.writeText(t); }", message)
        time.sleep(0.1)   # let the async write settle
        page.keyboard.press("Control+a")
        time.sleep(0.05)  # let selection register before paste
        page.keyboard.press("Control+v")
        time.sleep(0.15)  # let the paste land in the DOM before nudging the framework
        _force_input_events(page)
        return True
    except Exception:
        return False


def _did_clear(el) -> bool:
    """True if the composer is now empty -- the universal "it actually sent" signal."""
    try:
        return not (el.evaluate("e => (e.innerText || e.value || '').trim()"))
    except Exception:
        return False


def _verify_sent(page, site: dict, el, pre_counts: dict, timeout: float = 2.5) -> bool:
    """Fast post-submit check: did the composer clear, a stop button appear,
    or a new response element show up? This is deliberately short (~2.5s),
    distinct from _wait_and_read's long poll -- its job is letting
    _type_and_submit detect a no-op click (wrong/disabled button) and retry
    the next tier, not to wait out the model's actual response.
    """
    deadline = time.time() + timeout
    stop_sel = site.get("stop_btn")
    while time.time() < deadline:
        if _did_clear(el):
            return True
        if stop_sel:
            try:
                if page.locator(stop_sel).count() > 0:
                    return True
            except Exception:
                pass
        try:
            cur_counts = _response_counts(page, site)
            if any(cur_counts.get(sel, 0) > n for sel, n in pre_counts.items()):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _type_and_submit(page, site: dict, message: str, agent: str = "") -> None:
    el = _resolve_selector(page, site["input"])
    if el is None:
        raise RuntimeError(
            f"Input box not found on {page.url} (no VISIBLE match in {site['input']}). "
            "Is the page fully loaded and are you logged in?"
        )
    _save_debug(page, agent, "01_before_type")
    el.click()
    time.sleep(0.1)   # let focus settle before detecting element type
    tag = el.evaluate("el => el.tagName.toLowerCase()")
    # Narrowed to the actual Lexical attribute -- role="textbox" matches any
    # ARIA textbox (including plain contenteditable divs on non-Lexical
    # sites), which wrongly picked the newline-flattening fallback for them.
    is_lexical = el.evaluate("el => el.hasAttribute('data-lexical-editor')")

    if tag in ("textarea", "input"):
        # Native fill triggers React state correctly
        el.fill(message)
        el.dispatch_event("input")
        el.dispatch_event("change")
    else:
        # For all contenteditable (Lexical, ProseMirror, etc.) use clipboard paste.
        # execCommand('insertText') is deprecated in Chrome 90+ and silently drops
        # text in ProseMirror on current Chrome. keyboard.type() fires real Enter
        # events on every \n, submitting Lexical forms mid-message.
        # Clipboard paste avoids both problems. _clipboard_insert already fires
        # the input/change nudge internally; the fallback paths below need it too.
        if not _clipboard_insert(page, message):
            if is_lexical:
                # Lexical fallback: type without newlines to avoid premature submit
                page.keyboard.press("Control+a")
                page.keyboard.type(message.replace("\n", " "))
            else:
                # ProseMirror fallback: execCommand (deprecated but may still work)
                page.evaluate(
                    "text => { document.execCommand('selectAll'); "
                    "document.execCommand('insertText', false, text); }",
                    message,
                )
            _force_input_events(page)
    time.sleep(0.5)
    _save_debug(page, agent, "02_after_paste")

    pre_counts = _response_counts(page, site)
    submitted = False

    # Tier 1: configured send button. POLL for it to become enabled (up to
    # ~2s) instead of checking once -- large pastes take the framework a
    # moment to re-render and enable the button, and a single early check
    # was the original failure: it saw "disabled" and gave up immediately.
    send_sel = site.get("send_btn")
    if send_sel:
        try:
            btn = page.locator(send_sel)
            btn.first.wait_for(state="visible", timeout=3000)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if btn.count() > 0 and btn.first.is_enabled():
                    btn.first.click()
                    submitted = True
                    break
                time.sleep(0.2)
        except Exception:
            pass
        if submitted:
            _save_debug(page, agent, "03_after_send_click")
            if not _verify_sent(page, site, el, pre_counts):
                submitted = False  # click was a no-op -- composer never cleared

    # Tier 2: JS fallback. Must filter for actual send/submit semantics and
    # take the LAST match, not the first enabled+visible button found in DOM
    # order -- in every composer here the real send control is the last one
    # and starts disabled, so "first enabled button" was reliably the
    # attach/mic/tools control instead, clicking it and reporting success.
    if not submitted:
        try:
            clicked = page.evaluate("""() => {
                function isSend(b) {
                    const s = ((b.getAttribute('aria-label')||'') + ' ' +
                               (b.getAttribute('data-testid')||'') + ' ' +
                               (b.textContent||'')).toLowerCase();
                    return /send|submit|^ask$/.test(s) &&
                           !/attach|file|mic|voice|dictate|tool|model|stop/.test(s);
                }
                function tryClick(root) {
                    const btns = [...(root ? root.querySelectorAll('button') :
                                              document.querySelectorAll('button'))];
                    const sends = btns.filter(b => !b.disabled && b.offsetParent !== null && isSend(b));
                    const b = sends[sends.length - 1];
                    if (b) { b.click(); return true; }
                    return false;
                }
                // 1. Look inside the composer container wrapping the active element
                const active = document.activeElement;
                const composer = active &&
                    (active.closest('form') ||
                     active.closest('[class*="composer"]') ||
                     active.closest('[class*="input"]') ||
                     active.closest('[data-testid*="composer"]') ||
                     active.closest('[data-testid*="input"]'));
                if (composer && tryClick(composer)) return true;
                // 2. Anywhere on the page
                return tryClick(null);
            }""")
            if clicked:
                submitted = True
        except Exception:
            pass
        if submitted:
            _save_debug(page, agent, "03_after_send_click")
            if not _verify_sent(page, site, el, pre_counts):
                submitted = False

    # Tier 3: keyboard submit chords, each verified before trying the next --
    # a chord that just inserts a newline (wrong gesture for this site) must
    # not be allowed to masquerade as a successful send.
    if not submitted:
        for chord in ("Enter", "Control+Enter", "Meta+Enter"):
            try:
                el.click()
                el.press(chord)
            except Exception:
                continue
            if _verify_sent(page, site, el, pre_counts, timeout=1.5):
                submitted = True
                break

    if not submitted:
        _save_debug(page, agent, "04_send_not_verified", always=True)
        raise RuntimeError(
            "Message pasted but send did not trigger — composer never cleared "
            "and no new response/stop indicator appeared after all submit attempts."
        )


def _page_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return ""


DEBUG_DIR = Path(__file__).parent / "debug"

def _save_debug(page, agent: str, tag: str, always: bool = False) -> None:
    """Screenshot + a note on what actually happened, on disk for inspection.

    After this many rounds of guessing at selectors blind, the faster path
    is to look at exactly what Relay saw at the moment something went wrong
    instead of proposing another speculative selector tweak.

    Happy-path captures are gated behind RELAY_DEBUG (a full-page screenshot
    costs ~300-700ms and fired 3x per send on the success path). Failure-path
    captures pass always=True so they are written regardless of the flag.
    """
    if not agent:
        return
    if not (always or DEBUG_ENABLED):
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = DEBUG_DIR / f"{agent}_{tag}_{stamp}"
        page.screenshot(path=str(base.with_suffix(".png")))
        base.with_suffix(".url.txt").write_text(page.url, encoding="utf-8")
    except Exception:
        pass


def _response_counts(page, site: dict) -> dict:
    resp_sels = site.get("response_sel", [])
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]
    counts = {}
    for sel in resp_sels:
        try:
            counts[sel] = page.locator(sel).count()
        except Exception:
            counts[sel] = 0
    return counts


def _wait_and_read(page, site: dict, timeout: int, pre_len: int = 0,
                    pre_counts: dict | None = None, agent: str = "") -> str:
    """Wait for the AI response then return it.

    Strategy:
    1. Try the site's stop-button selector to detect generation start/end.
    2. If that selector doesn't match (DOM drift), wait for a NEW response
       element to actually appear in the DOM before doing anything else.
       This is the critical fix: a naive "page text stopped changing" check
       can't tell a multi-second "thinking" pause (no new element yet, text
       flat) from "done responding" (also flat) -- it was firing during the
       thinking pause and returning the user's own echoed message instead
       of the reply. Waiting for element-count to increase first means the
       stability check never even starts until real content exists.
    3. Extract response: try CSS selectors first, then return the page-text
       diff (everything new since before we typed) as a last resort.
    """
    stop_sel = site.get("stop_btn")
    appeared = False
    if stop_sel:
        for _ in range(10):
            try:
                page.wait_for_selector(stop_sel, timeout=500)
                appeared = True
                break
            except Exception:
                time.sleep(0.3)
        if appeared:
            try:
                page.wait_for_selector(stop_sel, state="hidden", timeout=timeout * 1000)
            except Exception:
                pass
            # Fast path: if the response selectors still match (the common
            # case), we're done. If they're stale (a UI redesign moved the
            # class names), don't jump straight to the unreliable raw
            # page-text diff below -- fall through into the SAME element-
            # count/stability logic used when no stop button matched at all.
            resp = _read_last_response(page, site)
            if resp:
                return resp
            appeared = False

    if not appeared:
        deadline = time.time() + timeout

        # Wait for a NEW response element before checking stability at all.
        new_el_seen = False
        if pre_counts:
            while time.time() < deadline:
                cur_counts = _response_counts(page, site)
                if any(cur_counts.get(sel, 0) > n for sel, n in pre_counts.items()):
                    new_el_seen = True
                    break
                time.sleep(0.5)
            if not new_el_seen:
                _save_debug(page, agent, "03_no_new_element_appeared", always=True)

        # Now check page-text stability (skips the thinking-pause window above).
        # The +80 floor only matters BEFORE new_el_seen, to rule out the user's
        # own echoed message satisfying stability during the thinking pause.
        # Once the element-count gate already proved a genuine new response
        # exists, requiring another +80 chars of growth on top of that was
        # wrong: short replies (handshake-style "Confirmed.", "[NAME] online.")
        # never reach it and always burn the full timeout despite having
        # finished immediately. Drop the floor to a tiny margin once gated.
        time.sleep(1)
        floor = pre_len + 80 if not new_el_seen else len(_page_text(page)) - 8
        prev_len = len(_page_text(page))
        stable = 0
        while time.time() < deadline:
            cur_len = len(_page_text(page))
            if cur_len == prev_len and cur_len > floor:
                stable += 1
                # 2 flat reads (was 3): the element-count gate above already
                # proved a real response exists, so the stability check only
                # needs to confirm growth has stopped, not survive a long tail.
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_len = cur_len
            time.sleep(1)

    # --- Extract the response text ---

    # 1. Try specific CSS selectors (fast, precise when selectors are current)
    resp = _read_last_response(page, site)
    if resp:
        return resp

    # 2. Page-text diff: everything new since before we typed, cleaned of UI chrome.
    if pre_len > 0:
        full = _page_text(page)
        new_text = full[pre_len:].strip()
        if new_text:
            return _clean_diff(new_text)

    _save_debug(page, agent, "04_no_response_captured", always=True)
    return "[No response captured — check the browser window]"


# Long, specific phrases -- safe to substring-match since they're full
# sentences a genuine model response is very unlikely to contain verbatim.
_UI_CHROME_PHRASES = [
    "Claude is AI and can make mistakes",
    "Please double-check responses",
    "Want to be notified when Claude responds",
    "Claude responded:",
    "Gemini is AI and can make mistakes",
    "Keep in mind Gemini",
    "Chats are reviewed",
    "Use microphone",
    "Terms and Privacy",
    "ChatGPT can make mistakes",
    "Always verify important information",
]

# Short, generic button/label words -- substring matching against these
# silently dropped any real response line that happened to contain the word
# (e.g. "Copy this snippet", "Share the dataset"). Exact whole-line match only.
_UI_CHROME_EXACT = {s.lower() for s in [
    "Copy", "Share", "Notify", "New conversation",
    "Sonnet", "Opus", "Haiku", "Google AI",
]}

def _clean_diff(text: str) -> str:
    """Strip UI chrome from a page-text diff and return the response content."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower() in _UI_CHROME_EXACT:
            continue
        if any(pat in stripped for pat in _UI_CHROME_PHRASES):
            continue
        clean.append(line)
    # Drop leading/trailing blank lines and return
    result = "\n".join(clean).strip()
    # If everything was stripped (very short response), return original
    return result if result else text.strip()


def _read_last_response(page, site: dict) -> str:
    resp_sels = site.get("response_sel", [])
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]
    for sel in resp_sels:
        try:
            elements = page.locator(sel).all()
            if elements:
                text = elements[-1].inner_text().strip()
                if text:  # any non-empty text from a specific selector is valid
                    return text
        except Exception:
            continue
    return ""


# ── BrowserManager ──────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Connects to an already-running Chrome via CDP (port 9222).
    Agents map to existing tabs by URL match — no new Chrome windows opened.
    """

    def __init__(self):
        self._cmd_q   = queue.Queue()
        self._ready_q = queue.Queue()
        self._pages: dict[str, object] = {}  # agent_name -> page
        self._tab_ids: dict[str, str] = {}   # agent_name -> bound CDP targetId
        self._tabid_cache: dict[int, str] = {}  # id(page) -> targetId (computed once)
        self._cdp_sessions: list = []        # keep refs alive so listeners aren't GC'd
        self._browser = None
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="browser-manager"
        )
        self._thread.start()
        status, val = self._ready_q.get(timeout=15)
        if status == "error":
            raise RuntimeError(val)

    # ── Thread ───────────────────────────────────────────────────────────────

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                try:
                    self._browser = pw.chromium.connect_over_cdp(CDP_URL)
                    # Hide webdriver flag and arm auto-resume on every page globally
                    for ctx in self._browser.contexts:
                        try:
                            ctx.add_init_script(
                                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                            )
                            ctx.on("page", self._arm_auto_resume)
                        except Exception:
                            pass
                    self._ready_q.put(("ok", None))
                except Exception as e:
                    self._ready_q.put(("error", str(e)))
                    return

                while True:
                    try:
                        cmd = self._cmd_q.get(timeout=2)
                    except queue.Empty:
                        self._heartbeat()
                        continue

                    if cmd is None:
                        break

                    action, agent, payload, res_q = cmd
                    try:
                        if   action == "scan":       self._do_scan(res_q)
                        elif action == "assign":     self._do_assign(agent, payload, res_q)
                        elif action == "find_assign":self._do_find_assign(agent, payload, res_q)
                        elif action == "open_tab":   self._do_open_tab(agent, res_q)
                        elif action == "send":       self._do_send(agent, payload, res_q)
                        elif action == "send_batch": self._do_send_batch(payload, res_q)
                        elif action == "unassign":
                            self._pages.pop(agent, None)
                            self._tab_ids.pop(agent, None)
                            res_q.put(("ok", None))
                        elif action == "list":
                            res_q.put(("ok", list(self._pages.keys())))
                        elif action == "assignments":
                            res_q.put(("ok", self._assignments()))
                        elif action == "identity_ok":
                            res_q.put(("ok", self._identity_ok(agent)))
                        elif action == "front":
                            page = self._pages.get(agent)
                            if page is not None and not page.is_closed():
                                try:
                                    page.bring_to_front()
                                except Exception:
                                    pass
                            res_q.put(("ok", None))
                        elif action == "has":
                            page = self._pages.get(agent)
                            res_q.put(("ok", page is not None and not page.is_closed()))
                    except Exception as e:
                        res_q.put(("error", str(e)))
        except Exception as e:
            try:
                self._ready_q.put(("error", str(e)))
            except Exception:
                pass

    def _heartbeat(self):
        dead = [n for n, p in self._pages.items() if p.is_closed()]
        for n in dead:
            self._pages.pop(n, None)
            self._tab_ids.pop(n, None)

    def _target_id(self, page) -> Optional[str]:
        """Stable CDP targetId for a tab — survives reloads/route changes, so it
        is the binding key (never a list index, which shifts when tabs open or
        close). Computed once per page and cached; a tab that is closed and
        reopened mints a NEW targetId, which is exactly how identity_ok detects
        a stale binding instead of silently re-pairing to a stray tab.
        """
        if page is None:
            return None
        k = id(page)
        if k in self._tabid_cache:
            return self._tabid_cache[k]
        tid = None
        try:
            cdp = page.context.new_cdp_session(page)
            try:
                tid = cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except Exception:
            tid = None
        self._tabid_cache[k] = tid
        return tid

    def _page_for_tab_id(self, tab_id: str):
        for page in self._all_pages():
            try:
                if self._target_id(page) == tab_id:
                    return page
            except Exception:
                continue
        return None

    def _assignments(self) -> dict:
        """agent -> bound tab_id, computed live so the UI renders ownership from
        the manager's reverse view (single source of truth), never a separate
        UI-side guess that can disagree with the actual bindings."""
        out = {}
        for ag, page in self._pages.items():
            try:
                if page is not None and not page.is_closed():
                    out[ag] = self._tab_ids.get(ag) or self._target_id(page)
            except Exception:
                out[ag] = self._tab_ids.get(ag)
        return out

    def _identity_ok(self, agent: str) -> bool:
        """True only if the agent's bound tab is still present and still uniquely
        theirs. Fail-closed: a closed tab, a vanished targetId, or a tab now
        claimed by another agent all return False rather than rebinding."""
        page = self._pages.get(agent)
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        bound = self._tab_ids.get(agent)
        if bound is not None:
            # The live tab must still carry the targetId we bound to.
            if self._target_id(page) != bound:
                return False
            if self._page_for_tab_id(bound) is None:
                return False
        # No other agent may own this same page.
        return self._claimed_by_other(agent, page) is None

    def _all_pages(self) -> list:
        pages = []
        if self._browser:
            for ctx in self._browser.contexts:
                pages.extend(ctx.pages)
        return pages

    def _do_scan(self, res_q):
        tabs = []
        for p in self._all_pages():
            try:
                tabs.append({
                    "tab_id": self._target_id(p),
                    "url":    p.url,
                    "title":  p.title(),
                })
            except Exception:
                continue
        res_q.put(("ok", tabs))

    def _claimed_by_other(self, agent: str, page) -> Optional[str]:
        """Return the name of a DIFFERENT agent already bound to this page, or None.

        Identity (`is`) comparison is correct here: Playwright caches one Page
        object per underlying tab inside the context, so the same tab yields the
        same Python object across _all_pages() snapshots.
        """
        for other, claimed in self._pages.items():
            if other != agent and claimed is page:
                return other
        return None

    def _do_assign(self, agent: str, tab_id: str, res_q):
        """Bind by stable tab_id (CDP targetId), never a list index — an index
        into a live snapshot shifts the instant a tab opens/closes, so "assign
        index 2" could silently land on a different page than the one clicked.
        """
        page = self._page_for_tab_id(tab_id)
        if page is None:
            res_q.put(("error", "That tab is no longer open — rescan and pick it again."))
            return
        # One tab per persona: refuse to bind a tab already owned by another
        # agent. Without this two Claude personas (all url_match "claude.ai")
        # could share one tab -- routing one persona's payload into the
        # other's system-primed window and crediting one bubble to both.
        other = self._claimed_by_other(agent, page)
        if other:
            res_q.put(("error", f"Tab already assigned to {other}"))
            return
        self._pages[agent] = page
        self._tab_ids[agent] = tab_id
        res_q.put(("ok", None))

    def _do_find_assign(self, agent: str, payload, res_q):
        """Atomically find and bind the first eligible tab in ONE snapshot.

        In a single _all_pages() pass: skip pages whose .url raises, skip pages
        containing exclude_substr, skip pages already claimed by another agent,
        and bind the first remaining page whose url contains url_match. Doing
        the scan-and-bind in one worker action (rather than scan-then-assign
        from app.py) closes the race where two personas pick the same tab
        between a shared scan and their separate assigns.
        """
        url_match, exclude_substr = payload
        for page in self._all_pages():
            try:
                url = page.url
            except Exception:
                continue
            if exclude_substr and exclude_substr in url:
                continue
            if self._claimed_by_other(agent, page):
                continue
            if url_match in url:
                self._pages[agent] = page
                self._tab_ids[agent] = self._target_id(page)
                res_q.put(("ok", url))
                return
        res_q.put(("ok", None))

    def _arm_auto_resume(self, page) -> None:
        """Auto-resume debugger pauses — keeps cdp ref alive so listener isn't GC'd."""
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Debugger.enable")
            cdp.on("Debugger.paused", lambda _: cdp.send("Debugger.resume"))
            self._cdp_sessions.append(cdp)  # prevent garbage collection
        except Exception:
            pass

    def _do_open_tab(self, agent: str, res_q):
        site = site_for_agent(agent)
        if not site:
            res_q.put(("error", f"No site configured for '{agent}'"))
            return
        try:
            ctx = self._browser.contexts[0]
            # new_page() triggers ctx.on("page") which arms auto-resume globally
            page = ctx.new_page()
            # "commit" returns as soon as the server responds — before JS runs.
            # Assign after goto so a failed navigation doesn't leave a broken entry.
            page.goto(site["url"], wait_until="commit", timeout=15_000)
            self._pages[agent] = page
            self._tab_ids[agent] = self._target_id(page)
            res_q.put(("ok", None))
        except Exception as e:
            try:
                page.close()
            except Exception:
                pass
            res_q.put(("error", str(e)))

    def _do_send(self, agent: str, payload, res_q):
        page = self._pages.get(agent)
        if page is None or page.is_closed():
            res_q.put(("error", f"No tab assigned to '{agent}' — assign one in Agents."))
            return
        # Pre-send identity check (fail-closed): the bound tab must still be
        # present and still uniquely this agent's. A stale binding halts the
        # send rather than typing one persona's prompt into another's tab.
        if not self._identity_ok(agent):
            res_q.put(("error", f"binding lost for '{agent}' — re-assign in Agents."))
            return
        site = site_for_agent(agent)
        if site is None:
            res_q.put(("error", f"Unknown site for agent '{agent}'"))
            return
        msg, timeout = payload
        # No bring_to_front(): CDP dispatches input events to a page's renderer
        # regardless of which tab is visually frontmost, so forcing a real
        # window-focus change (with its repaint cost) bought nothing here and is
        # the visible "switching tabs is slow" jank. The batch path already
        # dropped it for the same reason.
        pre_len = len(_page_text(page))           # snapshot before typing
        pre_counts = _response_counts(page, site)  # element counts before typing
        _type_and_submit(page, site, msg, agent)
        reply = _wait_and_read(page, site, timeout, pre_len, pre_counts, agent)
        res_q.put(("ok", reply))

    def _do_send_batch(self, payloads: dict, res_q):
        """Submit to all agents sequentially (fast), then poll all round-robin.

        payloads: {agent_name: (message_str, timeout_seconds)}
        Returns:  {agent_name: reply_str}  — errors embedded as "[Error: …]" strings.

        Wall-clock time ≈ slowest individual response, not the sum.
        All Playwright calls stay on this single thread — no thread-safety risk.
        """
        # Step 1: type + submit to every agent in turn (~1-2s each)
        state = {}
        for ag, (msg, tout) in payloads.items():
            page = self._pages.get(ag)
            site = site_for_agent(ag)
            if page is None or page.is_closed() or site is None:
                state[ag] = {"error": "No tab assigned or unknown site"}
                _save_debug(page, ag, "batch_02_no_tab", always=True) if page else None
                continue
            # Fail-closed identity check before typing: a stale/duplicated
            # binding must not land this agent's prompt in another's tab.
            if not self._identity_ok(ag):
                state[ag] = {"error": f"binding lost for '{ag}' — re-assign in Agents."}
                continue
            try:
                # No bring_to_front() here -- CDP dispatches input events
                # directly to a page's renderer regardless of which tab is
                # visually frontmost, so switching focus across N tabs in
                # rapid succession bought nothing functionally and was the
                # likely cause of the reported "switching tabs is super
                # slow and almost breaks the system": each switch forces a
                # real Chrome window-focus change with repaint/visibility-
                # change overhead, multiplied by every agent, every round.
                pre_len    = len(_page_text(page))
                pre_counts = _response_counts(page, site)
                _type_and_submit(page, site, msg, ag)
                state[ag] = {
                    "page": page, "site": site,
                    "pre_len": pre_len, "pre_counts": pre_counts,
                    "prev_len": pre_len,
                    "start": time.time(),
                    "deadline": time.time() + tout,
                    "stable": 0, "stop_seen": False, "new_el_seen": False,
                    "el_check_at": time.time() + 1,  # first element check after 1s
                }
            except Exception as e:
                state[ag] = {"error": str(e)}
                _save_debug(page, ag, "batch_02_type_error", always=True)

        results = {a: f"[Error: {s['error']}]" for a, s in state.items() if "error" in s}
        pending  = [a for a in state if "error" not in state[a]]

        # Step 2: round-robin poll until every agent has replied.
        # No initial sleep — check immediately so we don't miss a fast stop-button
        # that appears and disappears while we're sleeping.
        while pending:
            still = []
            for ag in pending:
                s = state[ag]
                page, site = s["page"], s["site"]

                # ── Deadline: try selector extraction before falling back to raw diff
                if time.time() > s["deadline"]:
                    resp = _read_last_response(page, site)
                    if not resp:
                        cur  = _page_text(page)
                        diff = cur[s["pre_len"]:].strip()
                        resp = _clean_diff(diff) if diff else ""
                    results[ag] = resp or "[Timed out]"
                    _save_debug(page, ag, "batch_05_timeout", always=True)
                    continue

                # ── Stop-button (cheapest: single locator count call)
                stop_sel = site.get("stop_btn")
                stop_now = False
                if stop_sel:
                    try:
                        stop_now = page.locator(stop_sel).count() > 0
                    except Exception:
                        pass
                    if stop_now:
                        s["stop_seen"] = True
                    if s["stop_seen"] and not stop_now:
                        resp = _read_last_response(page, site)
                        if not resp:
                            resp = _clean_diff(_page_text(page)[s["pre_len"]:].strip())
                        results[ag] = resp or "[No response captured]"
                        continue

                # ── Element-count gate — poll every 3s, no blind time fallback.
                # The single-send fix (707ef5e) waits for element count to actually
                # increase before the stability check starts.  Mirroring that here:
                # a longer blind timeout treats the symptom; removing it fixes the
                # cause — if no new element appears, there is no valid response yet.
                if not s["new_el_seen"]:
                    now = time.time()
                    if now >= s["el_check_at"]:
                        cur_counts = _response_counts(page, site)
                        if any(cur_counts.get(sel, 0) > n
                               for sel, n in s["pre_counts"].items()):
                            s["new_el_seen"] = True
                            # Floor for the stable-text check below: once the
                            # element gate is open we already know real content
                            # exists, so only require it to stop growing -- not
                            # another +80 chars on top, which short handshake-
                            # style replies ("Confirmed.", "[NAME] online.")
                            # never reach and would otherwise burn the full
                            # timeout despite having finished immediately.
                            s["stable_floor"] = len(_page_text(page)) - 8
                        else:
                            s["el_check_at"] = now + 1
                    still.append(ag)
                    s["prev_len"] = len(_page_text(page))
                    continue

                # ── Stable-text: only fires once element gate is open
                cur_len = len(_page_text(page))
                if cur_len == s["prev_len"] and cur_len > s["stable_floor"]:
                    s["stable"] += 1
                    # 2 flat reads (was 4): element-count gate already confirmed
                    # real content; this just confirms growth stopped. The
                    # stop-button branch above remains the primary done-signal
                    # for sites that expose it -- this is the fallback tail.
                    if s["stable"] >= 2:
                        resp = _read_last_response(page, site)
                        if not resp:
                            resp = _clean_diff(_page_text(page)[s["pre_len"]:].strip())
                        if resp:
                            results[ag] = resp
                        else:
                            results[ag] = "[No response captured]"
                            _save_debug(page, ag, "batch_04_no_response", always=True)
                        continue
                else:
                    s["stable"] = 0
                s["prev_len"] = cur_len
                still.append(ag)

            pending = still
            if pending:
                time.sleep(1)

        res_q.put(("ok", results))

    # ── Public API ────────────────────────────────────────────────────────────

    def _call(self, action: str, agent: str = "", payload=None, timeout: int = 40):
        res_q = queue.Queue()
        self._cmd_q.put((action, agent, payload, res_q))
        status, val = res_q.get(timeout=timeout)
        if status == "error":
            raise RuntimeError(val)
        return val

    def scan_tabs(self) -> list[dict]:
        return self._call("scan", timeout=5)

    def assign_tab(self, agent: str, tab_id: str) -> None:
        """Bind an agent to a tab by its stable tab_id (from scan_tabs())."""
        self._call("assign", agent, tab_id, timeout=5)

    def assignments(self) -> dict:
        """agent -> bound tab_id, live from the manager's reverse view."""
        try:
            return self._call("assignments", timeout=5)
        except Exception:
            return {}

    def identity_ok(self, agent: str) -> bool:
        """True if the agent's bound tab is still present and uniquely theirs."""
        try:
            return self._call("identity_ok", agent, timeout=5)
        except Exception:
            return False

    def bring_to_front(self, agent: str) -> None:
        """Surface the agent's bound tab so the operator can confirm it."""
        try:
            self._call("front", agent, timeout=5)
        except Exception:
            pass

    def find_and_assign(self, agent: str, url_match: str,
                        exclude_substr: Optional[str] = None) -> Optional[str]:
        """Atomically bind the first eligible tab matching url_match. Returns
        the bound url, or None if no eligible (unclaimed, non-excluded,
        matching) tab exists."""
        return self._call("find_assign", agent, (url_match, exclude_substr), timeout=5)

    def open_tab(self, agent: str) -> None:
        """Open a new tab in Chrome and navigate to the agent's site."""
        self._call("open_tab", agent, timeout=35)

    def send_message(self, agent: str, message: str, timeout: int = 120) -> str:
        return self._call("send", agent, (message, timeout), timeout=timeout + 30)

    def send_batch(self, payloads: dict, timeout: int = 120) -> dict:
        """Submit to all agents simultaneously and return {agent: reply}.

        payloads: {agent_name: message_str}  (timeout shared across all).
        Total wait ≈ slowest agent, not sum of all agents.
        """
        full_payloads = {a: (msg, timeout) for a, msg in payloads.items()}
        max_wait = timeout + len(payloads) * 5 + 30
        return self._call("send_batch", payload=full_payloads, timeout=max_wait)

    def has_agent(self, agent: str) -> bool:
        try:
            return self._call("has", agent, timeout=3)
        except Exception:
            return False

    def unassign(self, agent: str) -> None:
        try:
            self._call("unassign", agent, timeout=3)
        except Exception:
            pass

    def active_agents(self) -> list:
        try:
            return self._call("list", timeout=3)
        except Exception:
            return []

    def close(self) -> None:
        self._cmd_q.put(None)
        self._thread.join(timeout=5)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
