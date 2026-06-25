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
import uuid as _uuid
from pathlib import Path
from typing import Optional

try:
    from relay_log import get_logger as _get_logger
    _log = _get_logger("relay.browser")
except Exception:
    import logging
    _log = logging.getLogger("relay.browser")

CDP_URL = "http://localhost:9222"

SITES: dict[str, dict] = {
    "claude": {
        "url":          "https://claude.ai/new",
        "url_match":    "claude.ai",
        "input":        [
            # Target the contenteditable ProseMirror *inside* the chat-input
            # wrapper first.  Selecting the wrapper div itself makes _did_clear
            # fail: the wrapper's innerText includes placeholder/UI text even
            # when the composer is empty, so _verify_sent always returns False.
            '[data-testid="chat-input"] div[contenteditable="true"]',
            '[data-testid="chat-input"]',
            'div[contenteditable="true"].ProseMirror',
            ".ProseMirror",
            'div[contenteditable="true"]',
        ],
        "stop_btn":     '[aria-label="Stop"]',
        "send_btn":     '[data-testid="send-button"]',
        # semantic_sel: one element per completed assistant turn.
        # [data-testid="assistant-message"] was the old attribute; Claude's
        # current DOM uses .font-claude-message as the per-turn wrapper.
        # Listing both: whichever matches contributes to the element count,
        # so a new reply always increases the sum regardless of which attr
        # Claude ships in a given UI version.
        "semantic_sel": [
            '[data-testid="assistant-message"]',
            '.font-claude-message',
        ],
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
        # semantic_sel: one element per assistant turn — already the response_sel
        "semantic_sel": ['[data-message-author-role="assistant"]'],
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
        # semantic_sel: model-response is the custom element wrapping each reply
        "semantic_sel": ["model-response"],
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
        # Multiple stop-button selectors: Perplexity ships different aria-labels
        # across versions ("Stop", "Stop generating", or none).  _verify_sent and
        # _do_send_batch both accept a list here and try each in order.
        "stop_btn": [
            'button[aria-label="Stop"]',
            'button[aria-label="Stop generating"]',
            'button[aria-label="Stop responding"]',
        ],
        "send_btn":     'button[aria-label="Submit"]',
        # Perplexity navigates to perplexity.ai/search/... on submit, so the
        # response DOM is on the NEW page, not the home page.  After navigation
        # these selectors target the answer section specifically.
        "response_sel": [
            # Specific data-testid selectors (most reliable when they match)
            '[data-testid="thread-assistant-response"] .prose',
            '[data-testid="answer"] .prose',
            '[data-testid="thread-assistant-response"]',
            '[data-testid="answer"]',
            # Prose typography containers — Perplexity uses Tailwind `.prose` for
            # the rendered markdown answer block.  Multiple matches → last wins
            # (newest in the thread, not the query display at the top).
            '.prose-base',
            '.prose',
            # Class-fragment fallbacks for versions that renamed the containers
            "[class*='answer'] .prose",
            "[class*='answer-content']",
            "[class*='response-content']",
            "[class*='answerBody']",
            "[class*='answer_body']",
            ".answer-text",
            "[class*='answer']",
            # Very broad last resort: readable text blocks excluding nav/header
            'main div[dir="auto"]',
            'div[dir="auto"]',
        ],
        # semantic_sel: one per completed answer block.
        # These data-testid values have not matched Perplexity's current DOM;
        # URL-change detection in _do_send_batch is the primary gate for
        # Perplexity. These are kept as a best-effort secondary check.
        "semantic_sel": [
            '[data-testid="thread-assistant-response"]',
            '[data-testid="answer"]',
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
        time.sleep(0.1)   # let the paste land in the DOM before nudging the framework
        _force_input_events(page)
        return True
    except Exception:
        return False


def _did_clear(el, page=None) -> bool:
    """True if the composer is now empty -- the universal "it actually sent" signal.

    Falls back to document.activeElement when el is stale (ProseMirror rebuilds
    its own DOM nodes during and after a large paste, invalidating the reference
    captured before the paste).
    """
    try:
        return not (el.evaluate("e => (e.innerText || e.value || '').trim()"))
    except Exception:
        if page:
            try:
                # Guard: only trust activeElement when it's a composer-like
                # element.  document.body / buttons / the full page would have
                # non-empty innerText and make this always return False even
                # after a successful send.
                return page.evaluate("""() => {
                    const e = document.activeElement;
                    if (!e) return false;
                    const isComposer = e.contentEditable === 'true'
                        || e.tagName === 'TEXTAREA'
                        || (e.tagName === 'INPUT'
                            && e.type !== 'button' && e.type !== 'submit');
                    if (!isComposer) return false;
                    return !(e.innerText || e.value || '').trim();
                }""")
            except Exception:
                pass
        return False


def _verify_sent(page, site: dict, el, pre_counts: dict, timeout: float = 4.0,
                 pre_url: str = "") -> bool:
    """Fast post-submit check: did the composer clear, a stop button appear,
    a URL navigation happen, or a new response element show up?

    Timeout raised to 4 s (was 2.5 s) — large ProseMirror pastes can take
    >2 s for Claude to show the stop button or clear the input.
    Falls back to document.activeElement for the clear-check when el is stale.
    """
    deadline  = time.time() + timeout
    stop_sels = site.get("stop_btn")
    if isinstance(stop_sels, str):
        stop_sels = [stop_sels]
    while time.time() < deadline:
        # Navigation = form submitted (e.g. Perplexity navigates after send)
        if pre_url and page.url != pre_url:
            return True
        if _did_clear(el, page):
            return True
        if stop_sels:
            for ss in stop_sels:
                try:
                    if page.locator(ss).count() > 0:
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
    time.sleep(0.2)
    _save_debug(page, agent, "02_after_paste")

    # Re-resolve element after paste — ProseMirror rebuilds its DOM nodes during
    # large clipboard inserts, making the original `el` reference stale before
    # we even try to click the send button.
    try:
        fresh = _resolve_selector(page, site["input"], timeout=800, prefer_latest=False)
        if fresh:
            el = fresh
    except Exception:
        pass  # keep original el if re-resolve fails

    pre_counts = _response_counts(page, site)
    pre_url    = page.url  # capture URL for navigation-detection in _verify_sent
    submitted  = False

    # Tier 1: configured send button. POLL for it to become enabled (up to
    # ~3s) instead of checking once -- large pastes take the framework a
    # moment to re-render and enable the button, and a single early check
    # was the original failure: it saw "disabled" and gave up immediately.
    send_sel = site.get("send_btn")
    if send_sel:
        try:
            btn = page.locator(send_sel)
            btn.first.wait_for(state="visible", timeout=3000)
            deadline = time.time() + 3.0  # was 2.0 — some frameworks debounce longer
            while time.time() < deadline:
                if btn.count() > 0 and btn.first.is_enabled():
                    btn.first.click()
                    submitted = True
                    break
                time.sleep(0.15)
        except Exception:
            pass
        if submitted:
            _save_debug(page, agent, "03_after_send_click")
            if not _verify_sent(page, site, el, pre_counts, pre_url=pre_url):
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
            if not _verify_sent(page, site, el, pre_counts, pre_url=pre_url):
                submitted = False

    # Tier 3: keyboard submit chords, each verified before trying the next --
    # a chord that just inserts a newline (wrong gesture for this site) must
    # not be allowed to masquerade as a successful send.
    if not submitted:
        for chord in ("Enter", "Control+Enter", "Meta+Enter"):
            try:
                # Re-resolve element — may have become stale if ProseMirror
                # rebuilt the node during Tier 1/2 attempts.
                cur_el = _resolve_selector(page, site["input"], timeout=500,
                                           prefer_latest=False) or el
                cur_el.click()
                cur_el.press(chord)
            except Exception:
                continue
            if _verify_sent(page, site, el, pre_counts, timeout=2.0, pre_url=pre_url):
                submitted = True
                break

    if not submitted:
        _save_debug(page, agent, "04_send_not_verified")
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


def _semantic_counts(page, site: dict) -> dict:
    """Count completed assistant-turn containers using semantic selectors.

    Semantic selectors target the top-level turn container (one per reply),
    not child elements like prose blocks or code nodes. Falls back to
    _response_counts when no semantic_sel is defined for the site.
    """
    sels = site.get("semantic_sel", [])
    if not sels:
        return _response_counts(page, site)
    if isinstance(sels, str):
        sels = [sels]
    counts = {}
    for sel in sels:
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
                _save_debug(page, agent, "03_no_new_element_appeared")

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

# Short, generic button/label words -- exact whole-line match only.
_UI_CHROME_EXACT = {s.lower() for s in [
    "Copy", "Share", "Notify", "New conversation",
    "Share Copy", "Copy Share",
    "Google AI",
]}

# Model-selector labels appear as "Sonnet 4.6 Low", "Opus 4.8 Fast", etc. --
# the version suffix varies so we prefix-match instead of exact-match.
_UI_CHROME_PREFIXES = tuple(s.lower() for s in ["Sonnet", "Opus", "Haiku"])

def _clean_diff(text: str) -> str:
    """Strip UI chrome from a page-text diff and return the response content."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        sl = stripped.lower()
        if sl in _UI_CHROME_EXACT:
            continue
        if sl.startswith(_UI_CHROME_PREFIXES):
            continue
        if any(pat in stripped for pat in _UI_CHROME_PHRASES):
            continue
        clean.append(line)
    result = "\n".join(clean).strip()
    return result if result else text.strip()


def _make_envelope(agent: str, payload: str, pre_counts: dict,
                   page, site, pre_method: str = "fallback",
                   pre_url: str = "", grace_retries: int = 3,
                   grace_interval: float = 0.25) -> dict:
    """Wrap a scraped response in a lineage envelope.

    Captures pre/post DOM element counts so app.py can reject stale reads
    (cases where the response selector matched an OLD element instead of the
    one the agent just generated). Agents never see this structure — app.py
    strips it before cross-injection and display.

    pre_url: the page URL before submission.  If the page navigated (e.g.
    Perplexity home → search result) this counts as validated=True even when
    semantic element counts are both 0, because the navigation itself is
    unambiguous proof a submission happened.

    grace_retries/grace_interval: the post-submission element count used to
    be sampled exactly once. A site's stop-button-hidden event and the new
    message's DOM node landing are not atomic -- under real load the new
    element can appear tens to a few hundred ms after the signal that
    triggered this call, comfortably past a single fixed-delay check. That
    was the actual mechanism behind the reported "stale DOM read, element
    count 8→8 / 0→0" false quarantines: a real reply was already captured by
    _read_last_response, but the one-shot semantic recount hadn't caught up
    yet. If the first sample shows no increase, re-sample a few more times
    (well under 1s total added wait) before concluding it's genuinely stale.
    """
    pre_idx     = sum(pre_counts.values())
    url_changed = bool(pre_url and page.url != pre_url)
    warnings: list[str] = []

    def _sample():
        try:
            if pre_method == "semantic" and site.get("semantic_sel"):
                counts = _semantic_counts(page, site)
                method = "semantic"
            else:
                counts = _response_counts(page, site)
                method = "fallback"
            return sum(counts.values()), method, None
        except Exception as exc:
            return pre_idx, "error", exc

    new_idx, capture_method, err = _sample()
    if err is not None:
        warnings.append(f"Post-count error: {err}")

    retries_used = 0
    if not url_changed and new_idx <= pre_idx and capture_method != "error":
        for _ in range(grace_retries):
            time.sleep(grace_interval)
            retries_used += 1
            new_idx, capture_method, err = _sample()
            if err is not None:
                warnings.append(f"Post-count error on grace retry: {err}")
                break
            if new_idx > pre_idx:
                warnings.append(
                    f"Validated on grace retry #{retries_used} "
                    f"({retries_used * grace_interval:.2f}s after first check)"
                )
                break

    delta = new_idx - pre_idx
    if delta > 1 and pre_method == "semantic":
        warnings.append(
            f"{delta} new semantic blocks appeared (pre={pre_idx}, post={new_idx}); "
            f"captured newest"
        )
    if url_changed and new_idx <= pre_idx:
        warnings.append(f"Validated via URL navigation ({pre_url!r} → {page.url!r}); "
                        f"semantic count did not increase ({pre_idx}→{new_idx})")

    validated = url_changed or new_idx > pre_idx

    # Even after the grace window, the count may genuinely never increase
    # (a real selector/DOM mismatch) while the payload text itself is real
    # and substantial -- that's a lower-confidence capture, not the same
    # thing as a genuinely empty/stale read. Distinguishing the two lets
    # app.py keep good content instead of discarding it just because one
    # specific counter never moved, while still flagging it for visibility.
    if not validated and payload and payload.strip() and not payload.strip().startswith(
        ("[Error:", "[Timed out]", "[No response captured", "[Not sent")
    ):
        capture_method = "semantic_unconfirmed"
        warnings.append(
            f"Semantic count never confirmed ({pre_idx}→{new_idx}) after "
            f"{retries_used} grace retries, but payload text looks real — "
            f"flagged lower-confidence rather than discarded"
        )

    return {
        "event_id":           str(_uuid.uuid4()),
        "round_id":           None,   # stamped by app.py after receipt
        "agent":              agent,
        "pre_response_count": pre_idx,
        "new_response_index": new_idx,
        "validated":          validated,
        "capture_method":     capture_method,
        "warnings":           warnings,
        "timestamp":          time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload":            payload,
    }


def _error_envelope(agent: str, message: str) -> dict:
    """Build an envelope for a setup/transport error (no DOM counts available)."""
    return {
        "event_id":           str(_uuid.uuid4()),
        "round_id":           None,
        "agent":              agent,
        "pre_response_count": 0,
        "new_response_index": 0,
        "validated":          False,
        "capture_method":     "error",
        "warnings":           [],
        "timestamp":          time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload":            message,
    }


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
        self._stopped = False  # set True by close() so alive goes False immediately
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="browser-manager"
        )
        self._thread.start()
        status, val = self._ready_q.get(timeout=8)  # was 15 — CDP connect is nearly instant
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
                    _log.error(f"CDP connection failed ({CDP_URL}) — {e}", exc_info=True)
                    self._ready_q.put(("error", str(e)))
                    return

                while True:
                    try:
                        cmd = self._cmd_q.get(timeout=0.3)
                    except queue.Empty:
                        self._heartbeat()
                        continue

                    if cmd is None:
                        break

                    action, agent, payload, res_q = cmd
                    try:
                        if   action == "scan":         self._do_scan(res_q)
                        elif action == "assign":       self._do_assign(agent, payload, res_q)
                        elif action == "open_tab":     self._do_open_tab(agent, res_q)
                        elif action == "send":         self._do_send(agent, payload, res_q)
                        elif action == "send_batch":   self._do_send_batch(payload, res_q)
                        elif action == "fast_health":  self._do_fast_health(agent, res_q)
                        elif action == "full_health":  self._do_full_health(agent, res_q)
                        elif action == "unassign":
                            self._pages.pop(agent, None)
                            res_q.put(("ok", None))
                        elif action == "list":
                            alive = [n for n, p in self._pages.items() if not p.is_closed()]
                            res_q.put(("ok", alive))
                        elif action == "duplicates":
                            # Group agents by page object identity (id(page)).
                            # Returns list of sets where each set has > 1 agent
                            # sharing the same physical tab.
                            from collections import defaultdict
                            groups: dict = defaultdict(list)
                            for n, p in self._pages.items():
                                if not p.is_closed():
                                    groups[id(p)].append(n)
                            dupes = [sorted(v) for v in groups.values() if len(v) > 1]
                            res_q.put(("ok", dupes))
                        elif action == "has":
                            page = self._pages.get(agent)
                            res_q.put(("ok", page is not None and not page.is_closed()))
                    except Exception as e:
                        _log.error(f"[action={action}, agent={agent}] Unhandled dispatch error — {e}", exc_info=True)
                        res_q.put(("error", str(e)))
        except Exception as e:
            _log.critical(f"BrowserManager thread crashed — {e}", exc_info=True)
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
        tabs = []
        for p in self._all_pages():
            try:
                url = p.url
            except Exception:
                continue          # page is completely broken — skip it
            try:
                title = p.title() # CDP call — can hang on an unresponsive tab
            except Exception:
                title = ""        # title is display-only; don't stall the whole scan
            tabs.append({"url": url, "title": title})
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

    def _do_fast_health(self, agent: str, res_q):
        """Check tab existence, URL, input visibility — no prompt sent."""
        page = self._pages.get(agent)
        site = site_for_agent(agent)
        result: dict = {"agent": agent, "ok": False, "reason": None}

        if page is None or page.is_closed():
            result["reason"] = "No tab assigned or tab was closed"
            res_q.put(("ok", result))
            return

        url_match = (site or {}).get("url_match", "")
        if url_match and url_match not in page.url:
            result["reason"] = f"URL mismatch — expected '{url_match}', got '{page.url[:60]}'"
            res_q.put(("ok", result))
            return

        if site is None:
            result["reason"] = "Unknown site for this agent"
            res_q.put(("ok", result))
            return

        try:
            el = _resolve_selector(page, site.get("input", []), timeout=2000, prefer_latest=False)
            if el is None:
                result["reason"] = "Input box not visible or not found"
                res_q.put(("ok", result))
                return
        except Exception as exc:
            result["reason"] = f"Input check error: {exc}"
            res_q.put(("ok", result))
            return

        result["ok"] = True
        result["url"] = page.url[:80]
        res_q.put(("ok", result))

    def _do_full_health(self, agent: str, res_q):
        """Send a tiny probe message and verify the response — slow path."""
        _PROBE = "Reply with exactly these two words and nothing else: health-ok"
        page = self._pages.get(agent)
        site = site_for_agent(agent)
        result: dict = {"agent": agent, "ok": False, "reason": None, "response": None}

        if page is None or page.is_closed() or site is None:
            result["reason"] = "No tab assigned or unknown site"
            res_q.put(("ok", result))
            return

        try:
            pre_counts = _semantic_counts(page, site)
            _type_and_submit(page, site, _PROBE, agent)
            response = _wait_and_read(page, site, timeout=30,
                                      pre_len=len(_page_text(page)),
                                      pre_counts=pre_counts, agent=agent)
            result["response"] = response
            # Accept any reply that includes "health" and isn't a failure sentinel
            is_ok = bool(response) and "health" in response.lower()
            is_ok = is_ok and not response.strip().startswith(
                ("[Error:", "[Timed out]", "[No response", "[Not sent")
            )
            result["ok"] = is_ok
            if not is_ok:
                result["reason"] = f"Unexpected response: {response[:100]}"
        except Exception as exc:
            _log.error(f"[agent={agent}] Full health check error — {exc}", exc_info=True)
            result["reason"] = f"Full health check error: {exc}"

        res_q.put(("ok", result))

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
                _save_debug(page, ag, "batch_02_no_tab") if page else None
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
                pre_url    = page.url  # capture BEFORE submit for URL-change detection
                pre_counts = _semantic_counts(page, site)
                pre_method = "semantic" if site.get("semantic_sel") else "fallback"
                _type_and_submit(page, site, msg, ag)
                # For sites that navigate on submit (e.g. Perplexity home → search
                # result), the pre_len measured on the old page is useless for the
                # text-diff fallback.  Re-capture from the NEW page if the URL changed.
                try:
                    if page.url != site.get("url", page.url).rstrip("/"):
                        page.wait_for_load_state("domcontentloaded", timeout=2000)
                        pre_len    = len(_page_text(page))
                        # NOTE: intentionally keep pre_counts from the old page
                        # (all zeros for Perplexity home) so URL-change validation
                        # in _make_envelope can detect that 0→0 happened on a
                        # site that navigates, not a genuine capture failure.
                except Exception:
                    pass
                state[ag] = {
                    "page": page, "site": site,
                    "pre_len": pre_len, "pre_counts": pre_counts,
                    "pre_method": pre_method,
                    "pre_url":  pre_url,  # URL before submit
                    "prev_len": pre_len,
                    "start": time.time(),
                    "deadline": time.time() + tout,
                    "stable": 0, "stop_seen": False, "new_el_seen": False,
                    "el_check_at": time.time() + 1,  # first element check after 1s
                }
            except Exception as e:
                _log.error(f"[agent={ag}] Batch type/submit error — {e}", exc_info=True)
                state[ag] = {"error": str(e)}
                _save_debug(page, ag, "batch_02_type_error")

        results = {
            a: _error_envelope(a, f"[Error: {s['error']}]")
            for a, s in state.items() if "error" in s
        }
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
                    results[ag] = _make_envelope(ag, resp or "[Timed out]",
                                                 s["pre_counts"], page, site,
                                                 s.get("pre_method", "fallback"),
                                                 s.get("pre_url", ""))
                    _save_debug(page, ag, "batch_05_timeout")
                    continue

                # ── Stop-button (cheapest: single locator count call)
                # stop_btn may be a string or list of strings (multiple aria-labels).
                stop_sels = site.get("stop_btn") or []
                if isinstance(stop_sels, str):
                    stop_sels = [stop_sels]
                stop_now = False
                for _ss in stop_sels:
                    try:
                        if page.locator(_ss).count() > 0:
                            stop_now = True
                            break
                    except Exception:
                        pass
                if stop_now:
                    s["stop_seen"] = True
                if s["stop_seen"] and not stop_now:
                        # Brief DOM-commit settle: the stop button can disappear
                        # a frame before the new message element is written to
                        # the DOM (ChatGPT 8→8 symptom). 350 ms is enough for
                        # any browser to flush its render queue after generation.
                        time.sleep(0.35)
                        resp = _read_last_response(page, site)
                        if not resp:
                            resp = _clean_diff(_page_text(page)[s["pre_len"]:].strip())
                        results[ag] = _make_envelope(ag, resp or "[No response captured]",
                                                     s["pre_counts"], page, site,
                                                     s.get("pre_method", "fallback"),
                                                     s.get("pre_url", ""))
                        continue

                # ── Element-count gate — also opens on URL change.
                # Sites like Perplexity navigate from the home page to a search
                # result page on submit.  Their semantic_sel selectors match the
                # search page but not the home page, so pre_counts is always 0.
                # A URL change is unambiguous proof that a new response context
                # exists; treat it as equivalent to a semantic-element increase.
                if not s["new_el_seen"]:
                    now = time.time()
                    pre_url = s.get("pre_url", "")
                    # URL-change gate: navigation = new response context
                    if pre_url and page.url != pre_url:
                        s["new_el_seen"]  = True
                        s["stable_floor"] = 0  # anything that stops growing is done
                    elif now >= s["el_check_at"]:
                        cur_counts = _semantic_counts(page, site)
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
                            s["el_check_at"] = now + 1.5
                    still.append(ag)
                    s["prev_len"] = len(_page_text(page))
                    continue

                # ── Stable-text: only fires once element gate is open
                cur_len = len(_page_text(page))
                if cur_len == s["prev_len"] and cur_len > s["stable_floor"]:
                    s["stable"] += 1
                    if s["stable"] >= 3:
                        resp = _read_last_response(page, site)
                        if not resp:
                            resp = _clean_diff(_page_text(page)[s["pre_len"]:].strip())
                        if not resp:
                            _save_debug(page, ag, "batch_04_no_response")
                        results[ag] = _make_envelope(ag, resp or "[No response captured]",
                                                     s["pre_counts"], page, site,
                                                     s.get("pre_method", "fallback"),
                                                     s.get("pre_url", ""))
                        continue
                else:
                    s["stable"] = 0
                s["prev_len"] = cur_len
                still.append(ag)

            pending = still
            if pending:
                time.sleep(0.75)

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
        try:
            return self._call("scan", timeout=10)  # was 5; title() can be slow
        except Exception:
            return []

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

    def list_agents(self) -> list[str]:
        """Return names of all currently-assigned, non-closed agents in one call."""
        try:
            return self._call("list", timeout=5)
        except Exception:
            return []

    def find_duplicate_tabs(self) -> list[list[str]]:
        """Return a list of agent-name groups that share a single Chrome tab.

        Each group is a sorted list of 2+ agent names assigned to the same page
        object.  Empty list means no conflicts.
        """
        try:
            return self._call("duplicates", timeout=5)
        except Exception:
            return []

    def fast_health_check(self, agent: str) -> dict:
        """Tab-only preflight: no prompt sent. Returns {ok, reason, url}."""
        return self._call("fast_health", agent, timeout=8)

    def full_health_check(self, agent: str) -> dict:
        """Send 'health-ok' probe and verify capture. Returns {ok, reason, response}."""
        return self._call("full_health", agent, timeout=60)

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
        """Signal the browser thread to exit. Returns immediately — does not block.

        _stopped is set True first so alive goes False right away, letting the
        Streamlit UI rerender without waiting for Playwright's CDP cleanup.
        The thread exits in the background within ~0.3s (next idle poll tick).
        """
        self._stopped = True
        self._pages.clear()          # release page refs — list_agents returns [] instantly
        self._cmd_q.put(None)        # wake the thread so it exits promptly

    @property
    def alive(self) -> bool:
        return not self._stopped and self._thread.is_alive()
