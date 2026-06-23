"""
browser_relay.py — Playwright browser automation for Relay.

Each browser agent runs in its own dedicated worker thread because
Playwright's sync API is not thread-safe. The worker owns the browser
for its lifetime and accepts commands via queues.
"""
import queue
import threading
import time
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).parent / "browser_profiles"

SITES: dict[str, dict] = {
    "chatgpt": {
        "url":          "https://chatgpt.com",
        "url_match":    "chatgpt.com",
        "new_chat":     "https://chatgpt.com/",
        "input":        [
            "#prompt-textarea",
            'div[contenteditable="true"][data-virtualkeyboard]',
        ],
        "stop_btn":     'button[data-testid="stop-button"]',
        "send_btn":     'button[data-testid="send-button"]',
        "response_sel": '[data-message-author-role="assistant"]',
    },
    "claude": {
        "url":          "https://claude.ai",
        "url_match":    "claude.ai",
        "new_chat":     "https://claude.ai/new",
        "input":        [
            'div[contenteditable="true"].ProseMirror',
            ".ProseMirror",
            'div[contenteditable="true"]',
        ],
        "stop_btn":     'button[aria-label="Stop"]',
        "send_btn":     'button[aria-label="Send message"]',
        "response_sel": [
            ".font-claude-message",
            '[data-is-streaming="false"] .prose',
            ".prose",
        ],
    },
    "gemini": {
        "url":          "https://gemini.google.com",
        "url_match":    "gemini.google.com",
        "new_chat":     "https://gemini.google.com/app",
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
        "url":          "https://www.perplexity.ai",
        "url_match":    "perplexity.ai",
        "new_chat":     "https://www.perplexity.ai/",
        "input":        [
            'textarea[placeholder*="Ask"]',
            'textarea[placeholder]',
            'div[contenteditable="true"]',
        ],
        "stop_btn":     'button[aria-label="Stop"]',
        "send_btn":     'button[aria-label="Submit"]',
        "response_sel": [
            ".prose",
            ".answer-text",
            "[class*='answer']",
        ],
    },
    "grok": {
        "url":          "https://grok.com",
        "url_match":    "grok.com",
        "new_chat":     "https://grok.com/",
        "input":        [
            'textarea[placeholder]',
            'div[contenteditable="true"]',
        ],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [
            ".message-content",
            "[class*='response']",
        ],
    },
    "copilot": {
        "url":          "https://copilot.microsoft.com",
        "url_match":    "copilot.microsoft.com",
        "new_chat":     "https://copilot.microsoft.com/",
        "input":        [
            'textarea[placeholder]',
            'div[contenteditable="true"]',
        ],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [
            ".response-message",
            "[class*='response']",
        ],
    },
}


def site_for_agent(agent_name: str) -> Optional[dict]:
    key_name = agent_name.lower().replace("_", "").replace(" ", "").replace("-", "")
    for key, cfg in SITES.items():
        if key in key_name:
            return {"key": key, **cfg}
    return None


def _resolve_selector(page, selectors, timeout: int = 8000) -> Optional[object]:
    """Try a list of selectors, return the first locator that finds an element."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _type_and_submit(page, site: dict, message: str) -> None:
    """Type message into the chat input and submit."""
    loc = _resolve_selector(page, site["input"])
    if loc is None:
        raise RuntimeError(
            f"Input box not found (tried: {site['input']}). "
            "Is the page loaded and are you logged in?"
        )

    el = loc.last
    el.click()
    time.sleep(0.1)

    # Clear existing text
    el.press("Control+a")
    time.sleep(0.05)
    el.press("Delete")
    time.sleep(0.1)

    # Type the message
    el.type(message, delay=15)
    time.sleep(0.2)

    # Submit: try send button first, fall back to Enter
    send_sel = site.get("send_btn")
    submitted = False
    if send_sel:
        try:
            btn = page.locator(send_sel)
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click()
                submitted = True
        except Exception:
            pass
    if not submitted:
        el.press("Enter")


def _wait_and_read(page, site: dict, timeout: int) -> str:
    """Wait for the AI to finish responding, return the last response text."""
    stop_sel = site.get("stop_btn")

    if stop_sel:
        # Wait for the stop button to appear (generation started)
        appeared = False
        for _ in range(20):
            try:
                page.wait_for_selector(stop_sel, timeout=1000)
                appeared = True
                break
            except Exception:
                time.sleep(0.5)

        if appeared:
            # Wait for it to disappear (generation done)
            try:
                page.wait_for_selector(stop_sel, state="hidden",
                                       timeout=timeout * 1000)
            except Exception:
                pass  # timed out — grab what's there
        else:
            # Stop button never appeared; maybe it responded instantly
            time.sleep(3)
    else:
        # No stop button — poll for stable content
        _wait_stable(page, site, timeout)

    return _read_last_response(page, site)


def _wait_stable(page, site: dict, timeout: int) -> None:
    """For sites without a stop button, wait until response text stops changing."""
    resp_sels = site["response_sel"]
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]

    deadline = time.time() + timeout
    prev = ""
    stable_count = 0
    time.sleep(2)

    while time.time() < deadline:
        current = _read_last_response(page, site)
        if current and current == prev:
            stable_count += 1
            if stable_count >= 3:
                return
        else:
            stable_count = 0
        prev = current
        time.sleep(1)


def _read_last_response(page, site: dict) -> str:
    """Read the last assistant response from the page."""
    resp_sels = site["response_sel"]
    if isinstance(resp_sels, str):
        resp_sels = [resp_sels]

    for sel in resp_sels:
        try:
            elements = page.locator(sel).all()
            if elements:
                return elements[-1].inner_text().strip()
        except Exception:
            continue
    return "[No response text found — check the browser window]"


class BrowserWorker:
    """
    Owns a single Playwright persistent browser context in a dedicated thread.
    The thread is the only one that ever touches Playwright objects.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._cmd_q    = queue.Queue()
        self._res_q    = queue.Queue()
        self._alive    = threading.Event()
        self._thread   = threading.Thread(
            target=self._run, daemon=True, name=f"browser-{agent_name}"
        )
        self._thread.start()
        # Block until browser is open (or error/timeout)
        try:
            status, val = self._res_q.get(timeout=60)
        except queue.Empty:
            raise RuntimeError(
                f"Browser for '{agent_name}' did not start within 60 s. "
                "Check that Playwright is installed: python -m playwright install chromium"
            )
        if status == "error":
            raise RuntimeError(val)

    # ── Internal thread ────────────────────────────────────────────────────────

    def _run(self):
        profile = PROFILES_DIR / self.agent_name
        profile.mkdir(parents=True, exist_ok=True)
        site = site_for_agent(self.agent_name)

        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    str(profile),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if site and site["url_match"] not in page.url:
                    page.goto(site["url"], wait_until="domcontentloaded", timeout=30_000)

                self._alive.set()
                self._res_q.put(("ok", None))

                while True:
                    try:
                        cmd = self._cmd_q.get(timeout=1)
                    except queue.Empty:
                        # Heartbeat: check the page is still alive
                        try:
                            page.title()
                        except Exception:
                            # Page/context closed externally
                            self._res_q.put(("error", "Browser window was closed"))
                            return
                        continue

                    if cmd is None:
                        break

                    action, payload = cmd
                    try:
                        if action == "send":
                            msg, timeout = payload
                            _type_and_submit(page, site, msg)
                            reply = _wait_and_read(page, site, timeout)
                            self._res_q.put(("ok", reply))

                        elif action == "status":
                            self._res_q.put(("ok", {
                                "url":   page.url,
                                "title": page.title(),
                            }))

                        elif action == "new_chat":
                            url = site.get("new_chat", site["url"]) if site else None
                            if url:
                                page.goto(url, wait_until="domcontentloaded",
                                          timeout=15_000)
                            self._res_q.put(("ok", None))

                    except Exception as e:
                        self._res_q.put(("error", str(e)))

        except Exception as e:
            self._res_q.put(("error", str(e)))
        finally:
            self._alive.clear()

    # ── Public API (called from Streamlit thread) ──────────────────────────────

    def send_message(self, message: str, timeout: int = 120) -> str:
        """Send a message and block until the AI finishes responding."""
        if not self.alive:
            raise RuntimeError("Browser worker is not running")
        self._cmd_q.put(("send", (message, timeout)))
        try:
            status, val = self._res_q.get(timeout=timeout + 30)
        except queue.Empty:
            raise RuntimeError(f"No response after {timeout + 30}s — check the browser window")
        if status == "error":
            raise RuntimeError(val)
        return val

    def new_chat(self) -> None:
        """Navigate to a fresh conversation."""
        self._cmd_q.put(("new_chat", None))
        try:
            self._res_q.get(timeout=20)
        except queue.Empty:
            pass

    def status(self) -> dict:
        """Get current page URL and title."""
        if not self.alive:
            return {}
        self._cmd_q.put(("status", None))
        try:
            _, val = self._res_q.get(timeout=5)
            return val or {}
        except Exception:
            return {}

    def close(self) -> None:
        """Shut down the browser and worker thread."""
        self._cmd_q.put(None)
        self._thread.join(timeout=10)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
