"""
browser_relay.py — Controls your existing Chrome via CDP.

Setup (one time):
  1. Right-click your Chrome shortcut → Properties
  2. Append  --remote-debugging-port=9222  to the Target field
  3. Open Chrome, log into claude.ai / chatgpt.com / etc.
  4. Click "Connect to Chrome" in Relay.

No separate browser windows. No profiles. No anti-bot fights.
"""
import queue
import threading
import time
from pathlib import Path
from typing import Optional

CDP_URL = "http://localhost:9222"

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

def _resolve_selector(page, selectors, timeout: int = 8000):
    """Resolve to a single, VISIBLE matching element.

    Hidden/decoy duplicates (legacy SEO textareas, off-screen mobile
    variants, etc.) are skipped -- matching a hidden element and typing
    into it is indistinguishable from doing nothing, which is exactly
    what broke Perplexity's input. Returns the element itself (already
    resolved), not a multi-match Locator -- callers no longer need `.last`.
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n - 1, -1, -1):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _type_and_submit(page, site: dict, message: str, agent: str = "") -> None:
    el = _resolve_selector(page, site["input"])
    if el is None:
        raise RuntimeError(
            f"Input box not found on {page.url} (no VISIBLE match in {site['input']}). "
            "Is the page fully loaded and are you logged in?"
        )
    _save_debug(page, agent, "01_before_type")
    el.click()
    tag = el.evaluate("el => el.tagName.toLowerCase()")
    is_lexical = el.evaluate(
        "el => el.hasAttribute('data-lexical-editor') || el.getAttribute('role') === 'textbox'"
    )

    if tag in ("textarea", "input"):
        # Native fill triggers React state correctly
        el.fill(message)
        el.dispatch_event("input")
        el.dispatch_event("change")
    elif is_lexical:
        # keyboard.type() fires a real Enter event on every \n, which submits
        # the Perplexity form mid-message. Use clipboard paste instead: the
        # whole text lands at once with no synthetic key events.
        # navigator.clipboard.writeText() is available in CDP-attached Chrome.
        try:
            page.evaluate("async t => await navigator.clipboard.writeText(t)", message)
            page.keyboard.press("Control+a")
            page.keyboard.press("Control+v")
        except Exception:
            # Fallback: type with newlines replaced by spaces (still avoids Enter)
            page.keyboard.press("Control+a")
            page.keyboard.type(message.replace("\n", " "))
    else:
        # ProseMirror and similar: execCommand is the reliable path
        page.evaluate(
            "text => { document.execCommand('selectAll'); "
            "document.execCommand('insertText', false, text); }",
            message,
        )
    time.sleep(0.5)

    submitted = False
    send_sel = site.get("send_btn")
    if send_sel:
        try:
            btn = page.locator(send_sel)
            btn.first.wait_for(state="visible", timeout=3000)
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click()
                submitted = True
        except Exception:
            pass
    if not submitted:
        # Find the submit button closest to the focused input, then fall back globally.
        try:
            clicked = page.evaluate("""() => {
                function tryClick(root) {
                    const btns = [...(root ? root.querySelectorAll('button') :
                                              document.querySelectorAll('button'))];
                    const b = btns.find(b => !b.disabled && b.offsetParent !== null);
                    if (b) { b.click(); return true; }
                    return false;
                }
                // 1. Look inside the composer container wrapping the active element
                const active = document.activeElement;
                const composer = active &&
                    (active.closest('form') ||
                     active.closest('[class*="composer"]') ||
                     active.closest('[class*="input"]'));
                if (composer && tryClick(composer)) return true;
                // 2. Labelled send/submit/ask buttons anywhere
                const labels = ['submit','send','ask'];
                for (const b of document.querySelectorAll('button')) {
                    const lbl = (b.getAttribute('aria-label') || b.textContent || '').toLowerCase();
                    if (labels.some(l => lbl.includes(l)) && !b.disabled && b.offsetParent !== null) {
                        b.click(); return true;
                    }
                }
                return false;
            }""")
            if clicked:
                submitted = True
        except Exception:
            pass
    if not submitted:
        el.press("Enter")


def _page_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return ""


DEBUG_DIR = Path(__file__).parent / "debug"

def _save_debug(page, agent: str, tag: str) -> None:
    """Screenshot + a note on what actually happened, on disk for inspection.

    After this many rounds of guessing at selectors blind, the faster path
    is to look at exactly what Relay saw at the moment something went wrong
    instead of proposing another speculative selector tweak.
    """
    if not agent:
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

    if not appeared:
        deadline = time.time() + timeout

        # Wait for a NEW response element before checking stability at all.
        if pre_counts:
            new_el_seen = False
            while time.time() < deadline:
                cur_counts = _response_counts(page, site)
                if any(cur_counts.get(sel, 0) > n for sel, n in pre_counts.items()):
                    new_el_seen = True
                    break
                time.sleep(0.5)
            if not new_el_seen:
                _save_debug(page, agent, "03_no_new_element_appeared")

        # Now check page-text stability (skips the thinking-pause window above).
        time.sleep(1)
        prev_len = len(_page_text(page))
        stable = 0
        while time.time() < deadline:
            cur_len = len(_page_text(page))
            if cur_len == prev_len and cur_len > pre_len + 80:
                stable += 1
                if stable >= 3:
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

    _save_debug(page, agent, "04_no_response_captured")
    return "[No response captured — check the browser window]"


_UI_CHROME = [
    # Claude footer / attribution
    "Claude is AI and can make mistakes",
    "Please double-check responses",
    "Sonnet", "Opus", "Haiku",
    "Want to be notified when Claude responds",
    "Claude responded:",
    # Gemini footer
    "Gemini is AI and can make mistakes",
    "Keep in mind Gemini",
    "Chats are reviewed",
    "Use microphone",
    "Google AI",
    "Terms and Privacy",
    # Perplexity / ChatGPT
    "ChatGPT can make mistakes",
    "Always verify important information",
    "Copy",
    "Share",
    "Notify",
    "New conversation",
]

def _clean_diff(text: str) -> str:
    """Strip UI chrome from a page-text diff and return the response content."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        if any(pat in stripped for pat in _UI_CHROME):
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
                        elif action == "open_tab":   self._do_open_tab(agent, res_q)
                        elif action == "send":       self._do_send(agent, payload, res_q)
                        elif action == "send_batch": self._do_send_batch(payload, res_q)
                        elif action == "unassign":
                            self._pages.pop(agent, None)
                            res_q.put(("ok", None))
                        elif action == "list":
                            res_q.put(("ok", list(self._pages.keys())))
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

    def _all_pages(self) -> list:
        pages = []
        if self._browser:
            for ctx in self._browser.contexts:
                pages.extend(ctx.pages)
        return pages

    def _do_scan(self, res_q):
        tabs = [{"url": p.url, "title": p.title()} for p in self._all_pages()]
        res_q.put(("ok", tabs))

    def _do_assign(self, agent: str, page_index: int, res_q):
        pages = self._all_pages()
        if 0 <= page_index < len(pages):
            self._pages[agent] = pages[page_index]
            res_q.put(("ok", None))
        else:
            res_q.put(("error", f"Tab index {page_index} out of range"))

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
        site = site_for_agent(agent)
        if site is None:
            res_q.put(("error", f"Unknown site for agent '{agent}'"))
            return
        msg, timeout = payload
        page.bring_to_front()
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
                continue
            try:
                page.bring_to_front()
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
                    "el_check_at": time.time() + 3,  # first element check after 3s
                }
            except Exception as e:
                state[ag] = {"error": str(e)}

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

                if time.time() > s["deadline"]:
                    cur = _page_text(page)
                    results[ag] = cur[s["pre_len"]:].strip() or "[Timed out]"
                    continue

                # Stop-button (cheapest: single locator count call)
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
                            resp = _page_text(page)[s["pre_len"]:].strip()
                        results[ag] = resp or "[No response captured]"
                        continue

                # Element-count gate — only poll every 3s to keep overhead low.
                # After 15s without a new element (selector drift), open the gate
                # anyway so stable-text can fire rather than waiting for timeout.
                if not s["new_el_seen"]:
                    now = time.time()
                    if now >= s["el_check_at"]:
                        cur_counts = _response_counts(page, site)
                        if any(cur_counts.get(sel, 0) > n
                               for sel, n in s["pre_counts"].items()):
                            s["new_el_seen"] = True
                        s["el_check_at"] = now + 3
                    # Fallback: open gate after 15s regardless
                    if not s["new_el_seen"] and (now - s["start"]) > 15:
                        s["new_el_seen"] = True

                # Stable-text: only count when gate is open
                cur_len = len(_page_text(page))
                if s["new_el_seen"] and cur_len == s["prev_len"] and cur_len > s["pre_len"] + 80:
                    s["stable"] += 1
                    if s["stable"] >= 4:
                        cur  = _page_text(page)
                        resp = _read_last_response(page, site)
                        if not resp:
                            resp = cur[s["pre_len"]:].strip()
                        results[ag] = resp or "[No response captured]"
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

    def assign_tab(self, agent: str, tab_index: int) -> None:
        self._call("assign", agent, tab_index, timeout=5)

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
