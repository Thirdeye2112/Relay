"""
browser_relay.py — Controls your existing Chrome via CDP.

Setup (one time):
  1. Right-click your Chrome shortcut → Properties
  2. Append  --remote-debugging-port=9222  to the Target field
  3. Open Chrome, log into claude.ai / chatgpt.com / etc.
  4. Click "Connect to Chrome" in Relay.

No separate browser windows. No profiles. No anti-bot fights.
"""
import asyncio
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
    # "claude_chat" is the normal claude.ai chat composer. It must NEVER match
    # claude.ai/code (the separate Claude Code IDE web view) -- that page has
    # none of the selectors below, so a tab assigned there would silently fail
    # every send with "Input box not found". url_match is deliberately just
    # "claude.ai" (not narrower) because exclusion of /code lives in
    # classify_tab_url()/find_tab_for_agent, which run BEFORE url_match is used
    # to decide whether a scanned tab is even offered to a chat persona.
    "claude_chat": {
        "url":          "https://claude.ai/new",
        "url_match":    "claude.ai",
        # Best-effort, UNVERIFIED against a live tab — generic file-input
        # guess used only by the opt-in "File mode" cross-context attachment
        # feature. _attach_file() never trusts this blindly: a failed attach
        # always falls back to typing the full inline text, never drops it.
        "file_input":   ['input[type="file"]'],
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
    # "claude_code" is claude.ai/code -- the Claude Code IDE web view, a
    # completely different product surface than the chat composer above.
    # Its DOM has not been verified against a live tab (no confirmed composer/
    # send/response selectors exist yet); this entry exists so an explicit
    # site_key override can select it and so fast/full health checks can
    # report "unsupported" with a clear reason instead of a generic failure.
    # Only reachable via an explicit site_key override -- never inferred from
    # an agent name (agents like claude_code_architect are chat personas
    # despite the name, and must keep matching claude_chat).
    "claude_code": {
        "url":            "https://claude.ai/code",
        "url_match":      "claude.ai/code",
        "experimental":   True,  # selectors below are best-effort guesses, unverified
        "file_input":     ['input[type="file"]'],
        "input":          [
            '[data-testid="chat-input"] div[contenteditable="true"]',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "stop_btn":     '[aria-label="Stop"]',
        "send_btn":     '[data-testid="send-button"]',
        "semantic_sel": ['[data-testid="assistant-message"]'],
        "response_sel": ['[data-testid="assistant-message"]'],
    },
    "chatgpt": {
        "url":          "https://chatgpt.com/",
        "url_match":    "chatgpt.com",
        "file_input":   ['input[type="file"]'],
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
        "file_input":   ['input[type="file"]'],
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
        "file_input":   ['input[type="file"]'],
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
        # The data-testid values have not been confirmed against current DOM;
        # URL-change detection in _do_send_batch remains the primary gate for
        # Perplexity regardless. Broader wildcard candidates added below as
        # additional secondary checks -- worth trying since the exact
        # data-testid strings may simply be stale, not because the structural
        # idea (one container per completed answer) is wrong.
        "semantic_sel": [
            '[data-testid="thread-assistant-response"]',
            '[data-testid="answer"]',
            "[data-testid*='answer']",
            "[data-testid*='thread'] article",
            "article",
            "[class*='answer']",
        ],
    },
    "grok": {
        "url":          "https://grok.com/",
        "url_match":    "grok.com",
        "file_input":   ['input[type="file"]'],
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".message-content", "[class*='response']"],
    },
    "copilot": {
        "url":          "https://copilot.microsoft.com/",
        "url_match":    "copilot.microsoft.com",
        "file_input":   ['input[type="file"]'],
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".response-message", "[class*='response']"],
    },
}


def site_for_agent(agent_name: str, site_key_override: str = "") -> Optional[dict]:
    """Resolve an agent to a SITES entry.

    site_key_override, when set, wins outright -- this is the only way an
    agent can ever resolve to "claude_code". Without an override, matching is
    by substring on a normalized agent name, with one deliberate special case:
    "claude" (in any form -- claude_optimist, claude_code_architect, etc.)
    always maps to "claude_chat", never to "claude_code". This keeps existing
    personas whose names happen to contain "code" (claude_code_architect,
    claude_code_pragmatist -- chat personas with a systems-design personality,
    not drivers of the literal Claude Code product) from ever being silently
    routed to claude.ai/code.
    """
    if site_key_override:
        cfg = SITES.get(site_key_override)
        return {"key": site_key_override, **cfg} if cfg else None
    key = agent_name.lower().replace("_", "").replace(" ", "").replace("-", "")
    if "claude" in key:
        return {"key": "claude_chat", **SITES["claude_chat"]}
    for site_key, cfg in SITES.items():
        if site_key in ("claude_chat", "claude_code"):
            continue
        if site_key in key:
            return {"key": site_key, **cfg}
    return None


def classify_tab_url(url: str) -> str:
    """Classify a scanned browser tab's URL for tab-discovery/debug display.

    Returns one of: claude_chat, claude_code, claude_other, chatgpt, gemini,
    perplexity, grok, copilot, unsupported. claude_other catches claude.ai
    pages that are neither the chat composer nor /code (e.g. /settings) --
    a real Claude tab, just not one Relay can drive.
    """
    _NON_CHAT_CLAUDE_PATHS = ("/settings", "/account", "/projects", "/team")
    if "claude.ai/code" in url:
        return "claude_code"
    if "claude.ai" in url:
        if any(p in url for p in _NON_CHAT_CLAUDE_PATHS):
            return "claude_other"
        return "claude_chat"
    for site_key, cfg in SITES.items():
        if site_key in ("claude_chat", "claude_code"):
            continue
        if cfg["url_match"] in url:
            return site_key
    return "unsupported"


# ── Page helpers ────────────────────────────────────────────────────────────────
# Everything below talks to a page via playwright.async_api. All these helpers
# run on BrowserManager's single dedicated event loop (one OS thread, one CDP
# connection) -- async/await gives concurrency WITHIN that one loop (e.g. for
# the multi-agent wait phase in _do_send_batch) without ever opening a second
# connection or a thread pool. Do not call these from any other thread/loop.

_AGENT_MARKER_PREFIX = "RELAY_AGENT:"

async def _mark_page_for_agent(page, agent: str) -> None:
    """Stamp window.name so this tab's identity can be verified later.

    Multiple same-provider personas (e.g. CLAUDE_OPTIMIST, CLAUDE_SKEPTIC,
    CLAUDE_CODE_ARCHITECT all on claude.ai) cannot be told apart by URL
    matching alone. window.name persists on the page itself rather than
    only in Relay's own _pages dict, so a mismatch here means the actual
    browser tab no longer matches what Relay believes it assigned --
    catching that class of drift directly instead of inferring it from a
    confusing downstream symptom like a stale-DOM-read quarantine.
    """
    try:
        await page.evaluate("name => { window.name = name; }", f"{_AGENT_MARKER_PREFIX}{agent}")
    except Exception:
        pass  # marking is a safeguard, not a hard requirement -- never block assignment on it


async def _read_page_agent_marker(page) -> str:
    """Return the agent name a tab is marked for, or '' if unmarked/unreadable."""
    try:
        name = await page.evaluate("() => window.name || ''")
    except Exception:
        return ""
    if name.startswith(_AGENT_MARKER_PREFIX):
        return name[len(_AGENT_MARKER_PREFIX):]
    return ""


async def _verify_tab_identity(page, agent: str) -> tuple[bool, str]:
    """Check the tab's marker matches the agent we're about to send to.

    Returns (ok, marker). An empty marker (tab assigned before this feature
    existed, or evaluate failed) is treated as OK -- this is an added
    safeguard against drift, not a hard gate that breaks existing sessions.
    Only a marker that's PRESENT and WRONG is a hard mismatch.
    """
    marker = await _read_page_agent_marker(page)
    if not marker:
        return True, marker
    return marker == agent, marker


async def _resolve_selector(page, selectors, timeout: int = 1500, prefer_latest: bool = True):
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
            await page.wait_for_selector(sel, timeout=timeout)
            loc = page.locator(sel)
            n = await loc.count()
            ordering = range(n - 1, -1, -1) if prefer_latest else range(n)
            for i in ordering:
                candidate = loc.nth(i)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _force_input_events(page) -> None:
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
        await page.evaluate("""() => {
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


async def _insert_text_direct(page, el, message: str) -> bool:
    """Insert text via Playwright's dedicated Keyboard.insertText.

    Dispatches a real `input` event directly to the focused element -- no
    OS clipboard write, no clipboard-write permission grant, no async-write-
    then-paste race, no contention if multiple tabs touch the clipboard in
    close succession. This is the most direct insertion path Playwright
    offers for contenteditable/IME-aware editors and is tried FIRST, ahead
    of clipboard paste, since it sidesteps every clipboard-specific failure
    mode this codebase has hit outright rather than working around it.
    """
    try:
        await el.click()
        await page.keyboard.press("Control+a")  # select existing content so insert replaces it
        await page.keyboard.insert_text(message)
        return True
    except Exception:
        return False


async def _clipboard_insert(page, message: str) -> bool:
    """Write message to OS clipboard then paste into the focused element.

    Fallback for the rare case _insert_text_direct doesn't register
    correctly on a given site. execCommand('insertText') is deprecated in
    recent Chrome and silently fails for ProseMirror editors; clipboard
    paste works universally as a second-line fallback: the whole text
    lands in one event with no per-character key events and no newline-as-
    Enter risk. Returns True on success.
    """
    try:
        # Grant clipboard-write permission for this origin — harmless if already set.
        try:
            await page.context.grant_permissions(["clipboard-write"])
        except Exception:
            pass
        await page.evaluate("async t => { await navigator.clipboard.writeText(t); }", message)
        await asyncio.sleep(0.1)   # let the async write settle
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.05)  # let selection register before paste
        await page.keyboard.press("Control+v")
        await asyncio.sleep(0.1)   # let the paste land in the DOM before nudging the framework
        await _force_input_events(page)
        return True
    except Exception:
        return False


async def _did_clear(el, page=None) -> bool:
    """True if the composer is now empty -- the universal "it actually sent" signal.

    Falls back to document.activeElement when el is stale (ProseMirror rebuilds
    its own DOM nodes during and after a large paste, invalidating the reference
    captured before the paste).
    """
    try:
        text = await el.evaluate("e => (e.innerText || e.value || '').trim()")
        return not text
    except Exception:
        if page:
            try:
                # Guard: only trust activeElement when it's a composer-like
                # element.  document.body / buttons / the full page would have
                # non-empty innerText and make this always return False even
                # after a successful send.
                return await page.evaluate("""() => {
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


async def _verify_sent(page, site: dict, el, pre_counts: dict, timeout: float = 4.0,
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
        if await _did_clear(el, page):
            return True
        if stop_sels:
            for ss in stop_sels:
                try:
                    if await page.locator(ss).count() > 0:
                        return True
                except Exception:
                    pass
        try:
            cur_counts = await _response_counts(page, site)
            if any(cur_counts.get(sel, 0) > n for sel, n in pre_counts.items()):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def _verify_prompt_inserted(el, message: str) -> tuple[bool, str]:
    """Check the composer actually contains (a meaningful chunk of) the
    intended message before proceeding to click send.

    Catches a failure class distinct from "send didn't fire": the paste/
    fill step itself can silently not land (focus loss between click and
    paste, a clipboard write that raced, an editor that reset a stale DOM
    node) while the rest of the pipeline proceeds as if it had, and a wrong/
    disabled send button then gets blamed for something that never had real
    text behind it.

    Uses a prefix/suffix containment check rather than an exact match --
    rich-text editors can normalize whitespace, smart-quotes, etc., so a
    byte-perfect comparison would false-positive on genuinely correct
    insertions. Whitespace is collapsed on both sides before comparing.
    """
    try:
        actual = (await el.evaluate("e => (e.innerText || e.value || '').trim()") or "")
    except Exception as exc:
        return False, f"composer read failed: {exc}"

    if not actual:
        return False, "composer is empty after insertion"

    stripped = message.strip()
    check_len = min(60, len(stripped))
    if check_len == 0:
        return True, actual  # nothing to verify against

    norm_actual = " ".join(actual.split())
    norm_prefix = " ".join(stripped[:check_len].split())
    norm_suffix = " ".join(stripped[-check_len:].split())
    if norm_prefix in norm_actual or norm_suffix in norm_actual:
        return True, actual
    return False, actual


async def _attach_file(page, site: dict, file_path: str, agent: str = "") -> bool:
    """Best-effort attach of a local file via the composer's file input.

    site["file_input"] selectors are generic, UNVERIFIED guesses (no live
    tab has confirmed them against any provider) -- this exists purely to
    support the opt-in "File mode" cross-context feature. Callers must
    treat a False return as "use the full inline text instead", never as
    license to drop the content; this function itself never raises.
    """
    sels = site.get("file_input") or []
    if isinstance(sels, str):
        sels = [sels]
    for sel in sels:
        try:
            loc = page.locator(sel)
            if await loc.count() == 0:
                continue
            await loc.first.set_input_files(file_path)
            return True
        except Exception:
            continue
    await _save_debug(page, agent, "file_attach_failed")
    return False


async def _type_and_submit(page, site: dict, message: str, agent: str = "",
                            fast_mode: bool = False) -> dict:
    """Returns {"insertion_ms": int, "send_verify_ms": int} on success.
    Still raises RuntimeError on failure (callers unchanged) -- timing is
    additive, not a new control-flow path.

    fast_mode tightens the submit-tier verification deadlines below for
    the batch-send context specifically: _do_send_batch's Step 1 submits to
    every agent SEQUENTIALLY (deliberately -- everything here runs on
    BrowserManager's single event loop/single CDP connection, so a stuck
    agent waiting on Tier 1/2/3 deadlines could otherwise block every later
    agent in that loop from even starting to type, let alone respond).
    Single-send (_do_send, health checks) keeps the patient defaults since
    nothing else is waiting on it there.
    """
    _t0 = time.time()
    el = await _resolve_selector(page, site["input"])
    if el is None:
        raise RuntimeError(
            f"Input box not found on {page.url} (no VISIBLE match in {site['input']}). "
            "Is the page fully loaded and are you logged in?"
        )
    await _save_debug(page, agent, "01_before_type")
    await el.click()
    await asyncio.sleep(0.1)   # let focus settle before detecting element type
    tag = await el.evaluate("el => el.tagName.toLowerCase()")
    # Narrowed to the actual Lexical attribute -- role="textbox" matches any
    # ARIA textbox (including plain contenteditable divs on non-Lexical
    # sites), which wrongly picked the newline-flattening fallback for them.
    is_lexical = await el.evaluate("el => el.hasAttribute('data-lexical-editor')")

    if tag in ("textarea", "input"):
        # Native fill triggers React state correctly
        await el.fill(message)
        await el.dispatch_event("input")
        await el.dispatch_event("change")
    else:
        # For all contenteditable (Lexical, ProseMirror, etc.): try the most
        # direct path first (Playwright's own insertText), then progressively
        # less direct fallbacks. keyboard.type() fires real Enter events on
        # every \n, submitting Lexical forms mid-message -- avoided throughout.
        if await _insert_text_direct(page, el, message):
            await _force_input_events(page)  # cheap defensive nudge even though insertText already fires input
        elif not await _clipboard_insert(page, message):
            if is_lexical:
                # Lexical fallback: type without newlines to avoid premature submit
                await page.keyboard.press("Control+a")
                await page.keyboard.type(message.replace("\n", " "))
            else:
                # ProseMirror fallback: execCommand (deprecated but may still work)
                await page.evaluate(
                    "text => { document.execCommand('selectAll'); "
                    "document.execCommand('insertText', false, text); }",
                    message,
                )
            await _force_input_events(page)
    await asyncio.sleep(0.2)
    await _save_debug(page, agent, "02_after_paste")

    # Re-resolve element after paste — ProseMirror rebuilds its DOM nodes during
    # large clipboard inserts, making the original `el` reference stale before
    # we even try to click the send button.
    try:
        fresh = await _resolve_selector(page, site["input"], timeout=800, prefer_latest=False)
        if fresh:
            el = fresh
    except Exception:
        pass  # keep original el if re-resolve fails

    # Verify the intended text actually landed before attempting to send --
    # a no-op insertion (focus loss, clipboard race, stale node) must not be
    # allowed to proceed into the send tiers as if real text was waiting there.
    inserted_ok, actual_text = await _verify_prompt_inserted(el, message)
    if not inserted_ok:
        await _save_debug(page, agent, "02b_prompt_not_inserted")
        raise RuntimeError(
            f"Prompt insertion verification failed — composer does not contain "
            f"the intended message (composer has: {actual_text[:120]!r})"
        )

    _t_insert_done = time.time()
    pre_counts = await _response_counts(page, site)
    pre_url    = page.url  # capture URL for navigation-detection in _verify_sent
    submitted  = False

    # fast_mode (batch context) halves these -- see docstring above.
    wait_visible_ms = 1500 if fast_mode else 3000
    enable_poll_s   = 1.5  if fast_mode else 3.0
    verify1_s       = 2.0  if fast_mode else 4.0
    verify2_s       = 2.0  if fast_mode else 4.0
    verify3_s       = 1.0  if fast_mode else 2.0

    # Tier 1: configured send button. POLL for it to become enabled (up to
    # ~3s) instead of checking once -- large pastes take the framework a
    # moment to re-render and enable the button, and a single early check
    # was the original failure: it saw "disabled" and gave up immediately.
    send_sel = site.get("send_btn")
    if send_sel:
        try:
            btn = page.locator(send_sel)
            await btn.first.wait_for(state="visible", timeout=wait_visible_ms)
            deadline = time.time() + enable_poll_s
            while time.time() < deadline:
                if await btn.count() > 0 and await btn.first.is_enabled():
                    await btn.first.click()
                    submitted = True
                    break
                await asyncio.sleep(0.15)
        except Exception:
            pass
        if submitted:
            await _save_debug(page, agent, "03_after_send_click")
            if not await _verify_sent(page, site, el, pre_counts, timeout=verify1_s, pre_url=pre_url):
                submitted = False  # click was a no-op -- composer never cleared

    # Tier 2: JS fallback. Must filter for actual send/submit semantics and
    # take the LAST match, not the first enabled+visible button found in DOM
    # order -- in every composer here the real send control is the last one
    # and starts disabled, so "first enabled button" was reliably the
    # attach/mic/tools control instead, clicking it and reporting success.
    if not submitted:
        try:
            clicked = await page.evaluate("""() => {
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
            await _save_debug(page, agent, "03_after_send_click")
            if not await _verify_sent(page, site, el, pre_counts, timeout=verify2_s, pre_url=pre_url):
                submitted = False

    # Tier 3: keyboard submit chords, each verified before trying the next --
    # a chord that just inserts a newline (wrong gesture for this site) must
    # not be allowed to masquerade as a successful send.
    if not submitted:
        for chord in ("Enter", "Control+Enter", "Meta+Enter"):
            try:
                # Re-resolve element — may have become stale if ProseMirror
                # rebuilt the node during Tier 1/2 attempts.
                cur_el = await _resolve_selector(page, site["input"], timeout=500,
                                                  prefer_latest=False) or el
                await cur_el.click()
                await cur_el.press(chord)
            except Exception:
                continue
            if await _verify_sent(page, site, el, pre_counts, timeout=verify3_s, pre_url=pre_url):
                submitted = True
                break

    if not submitted:
        await _save_debug(page, agent, "04_send_not_verified")
        raise RuntimeError(
            "Message pasted but send did not trigger — composer never cleared "
            "and no new response/stop indicator appeared after all submit attempts."
        )

    _t_end = time.time()
    return {
        "insertion_ms":   int((_t_insert_done - _t0) * 1000),
        "send_verify_ms": int((_t_end - _t_insert_done) * 1000),
    }


async def _page_text(page) -> str:
    try:
        return await page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return ""


DEBUG_DIR = Path(__file__).parent / "debug"

async def _save_debug(page, agent: str, tag: str) -> None:
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
        await page.screenshot(path=str(base.with_suffix(".png")))
        base.with_suffix(".url.txt").write_text(page.url, encoding="utf-8")
    except Exception:
        pass


async def _response_counts(page, site: dict) -> dict:
    resp_sels = site.get("response_sel", [])
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]
    counts = {}
    for sel in resp_sels:
        try:
            counts[sel] = await page.locator(sel).count()
        except Exception:
            counts[sel] = 0
    return counts


async def _semantic_counts(page, site: dict) -> dict:
    """Count completed assistant-turn containers using semantic selectors.

    Semantic selectors target the top-level turn container (one per reply),
    not child elements like prose blocks or code nodes. Falls back to
    _response_counts when no semantic_sel is defined for the site.
    """
    sels = site.get("semantic_sel", [])
    if not sels:
        return await _response_counts(page, site)
    if isinstance(sels, str):
        sels = [sels]
    counts = {}
    for sel in sels:
        try:
            counts[sel] = await page.locator(sel).count()
        except Exception:
            counts[sel] = 0
    return counts


async def _wait_and_read(page, site: dict, timeout: int, pre_len: int = 0,
                          pre_counts: dict | None = None, agent: str = "",
                          pre_method: str = "fallback", pre_url: str = "") -> dict:
    """Wait for the AI response then return it wrapped in a lineage envelope.

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
    4. Wrap the captured text through _make_envelope -- the SAME grace-
       retried, semantic_unconfirmed-aware classification _do_send_batch's
       path already gets. This used to return a bare string with none of
       that hardening, leaving the synthesizer (send_message) and
       full_health_check exposed to the exact stale-DOM race fixed for
       batch sends. Callers (_do_send, _do_full_health, send_message) were
       updated for the new dict return type.
    """
    stop_sels = site.get("stop_btn") or []
    if isinstance(stop_sels, str):
        stop_sels = [stop_sels]
    appeared = False
    if stop_sels:
        # Poll for any stop button to appear (generation started)
        for _ in range(10):
            counts = [await page.locator(ss).count() for ss in stop_sels]
            if any(c > 0 for c in counts):
                appeared = True
                break
            await asyncio.sleep(0.5)
        if appeared:
            # Wait until no stop button is visible (generation done)
            deadline_stop = time.time() + timeout
            while time.time() < deadline_stop:
                counts = [await page.locator(ss).count() for ss in stop_sels]
                if not any(c > 0 for c in counts):
                    break
                await asyncio.sleep(0.5)
            # Fast path: if the response selectors still match (the common
            # case), we're done. If they're stale (a UI redesign moved the
            # class names), don't jump straight to the unreliable raw
            # page-text diff below -- fall through into the SAME element-
            # count/stability logic used when no stop button matched at all.
            resp = await _read_last_response(page, site)
            if resp:
                return await _make_envelope(agent, resp, pre_counts or {}, page, site, pre_method, pre_url)
            appeared = False

    if not appeared:
        deadline = time.time() + timeout

        # Wait for a NEW response element before checking stability at all.
        new_el_seen = False
        if pre_counts:
            while time.time() < deadline:
                cur_counts = await _semantic_counts(page, site)
                if any(cur_counts.get(sel, 0) > n for sel, n in pre_counts.items()):
                    new_el_seen = True
                    break
                await asyncio.sleep(0.5)
            if not new_el_seen:
                await _save_debug(page, agent, "03_no_new_element_appeared")

        # Now check page-text stability (skips the thinking-pause window above).
        # The +80 floor only matters BEFORE new_el_seen, to rule out the user's
        # own echoed message satisfying stability during the thinking pause.
        # Once the element-count gate already proved a genuine new response
        # exists, requiring another +80 chars of growth on top of that was
        # wrong: short replies (handshake-style "Confirmed.", "[NAME] online.")
        # never reach it and always burn the full timeout despite having
        # finished immediately. Drop the floor to a tiny margin once gated.
        await asyncio.sleep(1)
        floor = pre_len + 80 if not new_el_seen else len(await _page_text(page)) - 8
        prev_len = len(await _page_text(page))
        stable = 0
        while time.time() < deadline:
            cur_len = len(await _page_text(page))
            if cur_len == prev_len and cur_len > floor:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev_len = cur_len
            await asyncio.sleep(1)

    # --- Extract the response text ---

    # 1. Try specific CSS selectors (fast, precise when selectors are current)
    resp = await _read_last_response(page, site)
    if resp:
        return await _make_envelope(agent, resp, pre_counts or {}, page, site, pre_method, pre_url)

    # 2. Page-text diff: everything new since before we typed, cleaned of UI chrome.
    if pre_len > 0:
        full = await _page_text(page)
        new_text = full[pre_len:].strip()
        if new_text:
            return await _make_envelope(agent, _clean_diff(new_text), pre_counts or {}, page, site, pre_method, pre_url)

    await _save_debug(page, agent, "04_no_response_captured")
    return _error_envelope(agent, "[No response captured — check the browser window]")


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


async def _make_envelope(agent: str, payload: str, pre_counts: dict,
                          page, site, pre_method: str = "fallback",
                          pre_url: str = "", grace_retries: int = 3,
                          grace_interval: float = 0.25, timing: dict | None = None) -> dict:
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

    async def _sample():
        try:
            if pre_method == "semantic" and site.get("semantic_sel"):
                counts = await _semantic_counts(page, site)
                method = "semantic"
            else:
                counts = await _response_counts(page, site)
                method = "fallback"
            return sum(counts.values()), method, None
        except Exception as exc:
            return pre_idx, "error", exc

    new_idx, capture_method, err = await _sample()
    if err is not None:
        warnings.append(f"Post-count error: {err}")

    retries_used = 0
    if not url_changed and new_idx <= pre_idx and capture_method != "error":
        for _ in range(grace_retries):
            await asyncio.sleep(grace_interval)
            retries_used += 1
            new_idx, capture_method, err = await _sample()
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
        "site_key":           (site or {}).get("key", ""),
        "timing":             timing or {},
    }


def _error_envelope(agent: str, message: str, timing: dict | None = None,
                    failure_stage: str = "", site_key: str = "") -> dict:
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
        "site_key":           site_key,
        "timing":             timing or {},
        "failure_stage":      failure_stage,
    }


async def _read_last_response(page, site: dict) -> str:
    resp_sels = site.get("response_sel", [])
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]
    for sel in resp_sels:
        try:
            elements = await page.locator(sel).all()
            if elements:
                text = (await elements[-1].inner_text()).strip()
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

    Internals run on ONE dedicated OS thread (self._thread) which owns ONE
    asyncio event loop and ONE playwright.async_api CDP connection -- this is
    still the single-connection model the rest of this codebase depends on,
    just with async/await instead of blocking sync calls inside that one
    loop. async gives real concurrency *within* a single send_batch call
    (e.g. waiting on several agents' responses via asyncio.as_completed
    instead of a fixed time.sleep(0.75) round-robin poll) without opening a
    second connection or a thread pool -- do not "fix" this by adding either.
    The public methods below (_call and everything that uses it) are still
    plain synchronous calls from the caller's (Streamlit) thread; only the
    inside of the worker thread became async.
    """

    def __init__(self):
        self._cmd_q   = queue.Queue()
        self._ready_q = queue.Queue()
        self._pages: dict[str, object] = {}  # agent_name -> page
        self._site_overrides: dict[str, str] = {}  # agent_name -> explicit site_key (e.g. "claude_code")
        # Best-effort, UI-only progress snapshot for the in-flight send_batch call --
        # written by the worker thread as each agent finishes, read (unlocked) by the
        # Streamlit thread to update the "Waiting for…" spinner text. Not used for any
        # transport/safety decision, so plain dict read/write is fine here.
        self._batch_progress: dict[str, bool] = {}
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
        """Thread target: owns the asyncio event loop for this whole connection."""
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            _log.critical(f"BrowserManager thread crashed — {e}", exc_info=True)
            try:
                self._ready_q.put(("error", str(e)))
            except Exception:
                pass

    async def _run_async(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            try:
                self._browser = await pw.chromium.connect_over_cdp(CDP_URL)
                # Hide webdriver flag and arm auto-resume on every page globally
                for ctx in self._browser.contexts:
                    try:
                        await ctx.add_init_script(
                            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                        )
                        ctx.on("page", lambda page: asyncio.ensure_future(self._arm_auto_resume(page)))
                    except Exception:
                        pass
                self._ready_q.put(("ok", None))
            except Exception as e:
                _log.error(f"CDP connection failed ({CDP_URL}) — {e}", exc_info=True)
                self._ready_q.put(("error", str(e)))
                return

            loop = asyncio.get_event_loop()
            while True:
                try:
                    cmd = await loop.run_in_executor(None, self._cmd_q.get, True, 0.3)
                except queue.Empty:
                    self._heartbeat()
                    continue

                if cmd is None:
                    break

                action, agent, payload, res_q = cmd
                try:
                    if   action == "scan":         await self._do_scan(res_q)
                    elif action == "assign":       await self._do_assign(agent, payload, res_q)
                    elif action == "open_tab":     await self._do_open_tab(agent, payload, res_q)
                    elif action == "send":         await self._do_send(agent, payload, res_q)
                    elif action == "send_batch":   await self._do_send_batch(payload, res_q)
                    elif action == "fast_health":  await self._do_fast_health(agent, res_q)
                    elif action == "full_health":  await self._do_full_health(agent, res_q)
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

    def _resolve_site(self, agent: str) -> Optional[dict]:
        """site_for_agent, but honoring any explicit override set via assign_tab/open_tab."""
        return site_for_agent(agent, self._site_overrides.get(agent, ""))

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

    async def _do_scan(self, res_q):
        tabs = []
        for p in self._all_pages():
            try:
                url = p.url
            except Exception:
                continue          # page is completely broken — skip it
            try:
                # title()/evaluate have no built-in timeout -- an unresponsive
                # tab can hang the await indefinitely, and since _do_scan is
                # awaited sequentially by the single dispatch loop, that stalls
                # every other queued command (scan, assign, send...) behind it.
                # Bound each per-tab call so one bad tab can't freeze the worker.
                title = await asyncio.wait_for(p.title(), timeout=2.0)
            except Exception:
                title = ""        # title is display-only; don't stall the whole scan
            try:
                marker = await asyncio.wait_for(_read_page_agent_marker(p), timeout=2.0)
            except Exception:
                marker = ""
            tabs.append({
                "url": url, "title": title, "marker": marker,
                "classification": classify_tab_url(url),
            })
        res_q.put(("ok", tabs))

    async def _do_assign(self, agent: str, payload, res_q):
        if isinstance(payload, tuple):
            page_index, site_key = payload
        else:
            page_index, site_key = payload, ""
        pages = self._all_pages()
        if 0 <= page_index < len(pages):
            if site_key:
                self._site_overrides[agent] = site_key
            self._pages[agent] = pages[page_index]
            await _mark_page_for_agent(pages[page_index], agent)
            res_q.put(("ok", None))
        else:
            res_q.put(("error", f"Tab index {page_index} out of range"))

    async def _arm_auto_resume(self, page) -> None:
        """Auto-resume debugger pauses — keeps cdp ref alive so listener isn't GC'd."""
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Debugger.enable")

            async def _resume(_evt=None):
                try:
                    await cdp.send("Debugger.resume")
                except Exception:
                    pass

            cdp.on("Debugger.paused", lambda evt=None: asyncio.ensure_future(_resume(evt)))
            self._cdp_sessions.append(cdp)  # prevent garbage collection
        except Exception:
            pass

    async def _do_open_tab(self, agent: str, site_key: str, res_q):
        if site_key:
            self._site_overrides[agent] = site_key
        site = self._resolve_site(agent)
        if not site:
            res_q.put(("error", f"No site configured for '{agent}'"))
            return
        try:
            ctx = self._browser.contexts[0]
            # new_page() triggers ctx.on("page") which arms auto-resume globally
            page = await ctx.new_page()
            # "commit" returns as soon as the server responds — before JS runs.
            # Assign after goto so a failed navigation doesn't leave a broken entry.
            await page.goto(site["url"], wait_until="commit", timeout=15_000)
            self._pages[agent] = page
            await _mark_page_for_agent(page, agent)
            res_q.put(("ok", None))
        except Exception as e:
            try:
                await page.close()
            except Exception:
                pass
            res_q.put(("error", str(e)))

    async def _do_fast_health(self, agent: str, res_q):
        """Check tab existence, URL, input visibility, identity marker,
        semantic count — no prompt sent. Doubles as the per-agent transport
        preflight check: marker/semantic_count are reported regardless of
        whether the basic checks pass, so a preflight panel can show the
        full diagnostic picture even for an agent that's already failing.
        """
        page = self._pages.get(agent)
        site = self._resolve_site(agent)
        result: dict = {"agent": agent, "ok": False, "reason": None,
                        "marker": "", "marker_ok": None, "semantic_count": None}

        if page is None or page.is_closed():
            result["reason"] = "No tab assigned or tab was closed"
            res_q.put(("ok", result))
            return

        marker = await _read_page_agent_marker(page)
        result["marker"]    = marker
        result["marker_ok"] = (not marker) or (marker == agent)  # unmarked = OK, present-and-wrong = not

        if site is not None:
            try:
                result["semantic_count"] = sum((await _semantic_counts(page, site)).values())
            except Exception:
                pass

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
            el = await _resolve_selector(page, site.get("input", []), timeout=2000, prefer_latest=False)
            if el is None:
                if site.get("experimental"):
                    result["reason"] = "unsupported — claude.ai/code has no known composer/send/response selectors yet"
                else:
                    result["reason"] = "Input box not visible or not found"
                res_q.put(("ok", result))
                return
        except Exception as exc:
            result["reason"] = f"Input check error: {exc}"
            res_q.put(("ok", result))
            return

        if not result["marker_ok"]:
            result["reason"] = f"Tab identity mismatch — expected '{agent}', tab marked '{marker}'"
            res_q.put(("ok", result))
            return

        result["ok"] = True
        result["url"] = page.url[:80]
        res_q.put(("ok", result))

    async def _do_full_health(self, agent: str, res_q):
        """Send a tiny probe message and verify the response — slow path."""
        _PROBE = "Reply with exactly these two words and nothing else: health-ok"
        page = self._pages.get(agent)
        site = self._resolve_site(agent)
        result: dict = {"agent": agent, "ok": False, "reason": None, "response": None}

        if page is None or page.is_closed() or site is None:
            result["reason"] = "No tab assigned or unknown site"
            res_q.put(("ok", result))
            return

        try:
            pre_counts = await _semantic_counts(page, site)
            pre_url = page.url
            await _type_and_submit(page, site, _PROBE, agent)
            envelope = await _wait_and_read(page, site, timeout=30,
                                      pre_len=len(await _page_text(page)),
                                      pre_counts=pre_counts, agent=agent,
                                      pre_method="semantic", pre_url=pre_url)
            response = envelope.get("payload", "")
            result["response"] = response
            result["capture_method"] = envelope.get("capture_method")
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

    async def _do_send(self, agent: str, payload, res_q):
        page = self._pages.get(agent)
        if page is None or page.is_closed():
            res_q.put(("error", f"No tab assigned to '{agent}' — assign one in Agents."))
            return
        site = self._resolve_site(agent)
        if site is None:
            res_q.put(("error", f"Unknown site for agent '{agent}'"))
            return
        msg, timeout, attachment = payload
        ok, marker = await _verify_tab_identity(page, agent)
        if not ok:
            res_q.put(("error", f"[Error: TAB_IDENTITY_MISMATCH — expected '{agent}', got '{marker}']"))
            return
        await page.bring_to_front()
        context_mode = "inline"
        if attachment:
            if await _attach_file(page, site, attachment["path"], agent):
                msg = attachment["pointer"]
                context_mode = "file"
            else:
                context_mode = "file_failed_fallback_inline"
        pre_len = len(await _page_text(page))            # snapshot before typing
        pre_counts = await _semantic_counts(page, site)  # element counts before typing
        pre_method = "semantic" if site.get("semantic_sel") else "fallback"
        pre_url = page.url
        submit_timing = await _type_and_submit(page, site, msg, agent)
        _t_wait_start = time.time()
        envelope = await _wait_and_read(page, site, timeout, pre_len, pre_counts, agent, pre_method, pre_url)
        envelope["timing"] = {**submit_timing, "wait_response_ms": int((time.time() - _t_wait_start) * 1000)}
        envelope["site_key"] = site.get("key", "")
        envelope["context_mode"] = context_mode
        res_q.put(("ok", envelope))

    async def _do_send_batch(self, payloads: dict, res_q):
        """Submit to all agents sequentially (fast), then wait on all of them
        concurrently via asyncio.as_completed (each agent gets its own
        independent poll task on this same single event loop/CDP connection).

        payloads: {agent_name: (message_str, timeout_seconds, attachment_or_None)}
        Returns:  {agent_name: envelope dict}

        Wall-clock time ≈ slowest individual response, not the sum. Each
        per-agent poll task ticks on its own ~0.15s cadence and is reaped via
        asyncio.as_completed as soon as IT finishes -- replacing the previous
        fixed time.sleep(0.75) round-robin floor that gated every agent's
        detection latency on the slowest pass through the whole batch.
        Still a single asyncio event loop, one thread, one CDP connection --
        no thread pool, no second connection.
        """
        self._batch_progress = {ag: False for ag in payloads}
        # Step 1: type + submit to every agent in turn (~1-2s each)
        state = {}
        for ag, (msg, tout, attachment) in payloads.items():
            page = self._pages.get(ag)
            site = self._resolve_site(ag)
            if page is None or page.is_closed() or site is None:
                state[ag] = {"error": "No tab assigned or unknown site", "failure_stage": "assignment"}
                if page:
                    await _save_debug(page, ag, "batch_02_no_tab")
                continue
            id_ok, id_marker = await _verify_tab_identity(page, ag)
            if not id_ok:
                state[ag] = {"error": f"TAB_IDENTITY_MISMATCH — expected '{ag}', got '{id_marker}'",
                             "failure_stage": "assignment"}
                await _save_debug(page, ag, "batch_01_identity_mismatch")
                _log.warning(f"[agent={ag}] Tab identity mismatch — expected '{ag}', tab marked '{id_marker}'")
                continue
            context_mode = "inline"
            if attachment:
                if await _attach_file(page, site, attachment["path"], ag):
                    msg = attachment["pointer"]
                    context_mode = "file"
                else:
                    context_mode = "file_failed_fallback_inline"
            try:
                pre_len    = len(await _page_text(page))
                pre_url    = page.url
                pre_counts = await _semantic_counts(page, site)
                pre_method = "semantic" if site.get("semantic_sel") else "fallback"
                submit_timing = await _type_and_submit(page, site, msg, ag, fast_mode=True)
                try:
                    if page.url != pre_url:
                        await page.wait_for_load_state("domcontentloaded", timeout=2000)
                        pre_len    = len(await _page_text(page))
                except Exception:
                    pass
                state[ag] = {
                    "page": page, "site": site,
                    "pre_len": pre_len, "pre_counts": pre_counts,
                    "pre_method": pre_method,
                    "pre_url":  pre_url,
                    "prev_len": pre_len,
                    "submit_timing": submit_timing,
                    "context_mode": context_mode,
                    "start": time.time(),
                    "deadline": time.time() + tout,
                    "stable": 0, "stop_seen": False, "new_el_seen": False,
                    "el_check_at": time.time() + 1,
                }
            except Exception as e:
                _log.error(f"[agent={ag}] Batch type/submit error — {e}", exc_info=True)
                state[ag] = {"error": str(e), "failure_stage": "insertion"}
                await _save_debug(page, ag, "batch_02_type_error")

        results = {
            a: _error_envelope(a, f"[Error: {s['error']}]",
                               failure_stage=s.get("failure_stage", ""),
                               site_key=(s.get("site") or {}).get("key", ""))
            for a, s in state.items() if "error" in s
        }
        for a in results:
            self._batch_progress[a] = True

        pending_agents = [a for a in state if "error" not in state[a]]

        async def _poll_one(ag):
            """Independent per-agent wait loop -- same tiered detection logic
            as before (stop-button settle, element-count gate, stable-text,
            deadline fallback), just on its own ~0.15s cadence instead of a
            shared 0.75s pass so other agents' loops never gate this one's
            (or vice versa) detection latency.
            """
            s = state[ag]
            page, site = s["page"], s["site"]
            while True:
                if time.time() > s["deadline"]:
                    resp = await _read_last_response(page, site)
                    if not resp:
                        cur  = await _page_text(page)
                        diff = cur[s["pre_len"]:].strip()
                        resp = _clean_diff(diff) if diff else ""
                    env = await _make_envelope(ag, resp or "[Timed out]",
                                               s["pre_counts"], page, site,
                                               s.get("pre_method", "fallback"),
                                               s.get("pre_url", ""),
                                               timing={**s.get("submit_timing", {}),
                                                       "wait_response_ms": int((time.time() - s["start"]) * 1000)})
                    env["context_mode"] = s.get("context_mode", "inline")
                    await _save_debug(page, ag, "batch_05_timeout")
                    return ag, env

                stop_sels = site.get("stop_btn") or []
                if isinstance(stop_sels, str):
                    stop_sels = [stop_sels]
                stop_now = False
                for _ss in stop_sels:
                    try:
                        if await page.locator(_ss).count() > 0:
                            stop_now = True
                            break
                    except Exception:
                        pass
                if stop_now:
                    s["stop_seen"] = True
                if s["stop_seen"] and not stop_now:
                    await asyncio.sleep(0.35)
                    resp = await _read_last_response(page, site)
                    if not resp:
                        cur = await _page_text(page)
                        resp = _clean_diff(cur[s["pre_len"]:].strip())
                    env = await _make_envelope(ag, resp or "[No response captured]",
                                               s["pre_counts"], page, site,
                                               s.get("pre_method", "fallback"),
                                               s.get("pre_url", ""),
                                               timing={**s.get("submit_timing", {}),
                                                       "wait_response_ms": int((time.time() - s["start"]) * 1000)})
                    env["context_mode"] = s.get("context_mode", "inline")
                    return ag, env

                if not s["new_el_seen"]:
                    now = time.time()
                    pre_url = s.get("pre_url", "")
                    if pre_url and page.url != pre_url:
                        s["new_el_seen"]  = True
                        s["stable_floor"] = 0
                    elif now >= s["el_check_at"]:
                        cur_counts = await _semantic_counts(page, site)
                        if any(cur_counts.get(sel, 0) > n
                               for sel, n in s["pre_counts"].items()):
                            s["new_el_seen"] = True
                            s["stable_floor"] = len(await _page_text(page)) - 8
                        else:
                            s["el_check_at"] = now + 1.5
                    s["prev_len"] = len(await _page_text(page))
                    await asyncio.sleep(0.15)
                    continue

                cur_len = len(await _page_text(page))
                if cur_len == s["prev_len"] and cur_len > s["stable_floor"]:
                    s["stable"] += 1
                    if s["stable"] >= 3:
                        resp = await _read_last_response(page, site)
                        if not resp:
                            cur = await _page_text(page)
                            resp = _clean_diff(cur[s["pre_len"]:].strip())
                        if not resp:
                            await _save_debug(page, ag, "batch_04_no_response")
                        env = await _make_envelope(ag, resp or "[No response captured]",
                                                   s["pre_counts"], page, site,
                                                   s.get("pre_method", "fallback"),
                                                   s.get("pre_url", ""),
                                                   timing={**s.get("submit_timing", {}),
                                                           "wait_response_ms": int((time.time() - s["start"]) * 1000)})
                        env["context_mode"] = s.get("context_mode", "inline")
                        return ag, env
                else:
                    s["stable"] = 0
                s["prev_len"] = cur_len
                await asyncio.sleep(0.15)

        if pending_agents:
            tasks = [asyncio.ensure_future(_poll_one(ag)) for ag in pending_agents]
            for fut in asyncio.as_completed(tasks):
                ag, env = await fut
                results[ag] = env
                self._batch_progress[ag] = True

        res_q.put(("ok", results))

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

    def assign_tab(self, agent: str, tab_index: int, site_key: str = "") -> None:
        """site_key, when given, is recorded as an explicit override (e.g.
        "claude_code") so this agent's subsequent sends resolve to that site
        even though its name would otherwise infer "claude_chat"."""
        payload = (tab_index, site_key) if site_key else tab_index
        self._call("assign", agent, payload, timeout=5)

    def open_tab(self, agent: str, site_key: str = "") -> None:
        """Open a new tab in Chrome and navigate to the agent's site.
        site_key overrides the inferred site the same way assign_tab does."""
        self._call("open_tab", agent, site_key, timeout=35)

    def send_message(self, agent: str, message: str, timeout: int = 120,
                      attachment: Optional[dict] = None) -> dict:
        """Returns a lineage envelope (dict with a "payload" key), not a bare
        string -- same shape send_batch's per-agent results already use.

        attachment, when given, is {"path": str, "pointer": str} -- an
        opt-in "File mode" hook (see app.py's File mode toggle). If the
        file fails to attach (unverified selectors), the full `message`
        text is typed instead -- attachment never causes content loss.
        """
        return self._call("send", agent, (message, timeout, attachment), timeout=timeout + 30)

    def send_batch(self, payloads: dict, timeout: int = 120,
                    attachments: Optional[dict] = None) -> dict:
        """Submit to all agents simultaneously and return {agent: reply}.

        payloads: {agent_name: message_str}  (timeout shared across all).
        attachments: optional {agent_name: {"path": str, "pointer": str}} --
        see send_message's docstring; same fallback-never-drops guarantee.
        Total wait ≈ slowest agent, not sum of all agents.
        """
        attachments = attachments or {}
        full_payloads = {a: (msg, timeout, attachments.get(a)) for a, msg in payloads.items()}
        max_wait = timeout + len(payloads) * 5 + 30
        return self._call("send_batch", payload=full_payloads, timeout=max_wait)

    def batch_progress(self) -> dict:
        """Best-effort snapshot of {agent: done} for the in-flight (or most
        recent) send_batch call -- lets the caller poll which agents have
        already replied while still blocked waiting on the rest. Read from
        another thread without locking; this is UI feedback only, never used
        for a transport/safety decision, so a stale or torn read is harmless.
        """
        return dict(self._batch_progress)

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
