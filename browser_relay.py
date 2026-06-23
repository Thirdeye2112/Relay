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

# Selectors for known AI chat sites.
# These may need updating if a site redesigns its UI.
SITES: dict[str, dict] = {
    "chatgpt": {
        "url":           "https://chatgpt.com",
        "url_match":     "chatgpt.com",
        "input":         "#prompt-textarea",
        "stop_btn":      'button[data-testid="stop-button"]',
        "response_sel":  '[data-message-author-role="assistant"]',
    },
    "claude": {
        "url":           "https://claude.ai",
        "url_match":     "claude.ai",
        "input":         ".ProseMirror",
        "stop_btn":      'button[aria-label="Stop"]',
        "response_sel":  ".font-claude-message",
    },
    "gemini": {
        "url":           "https://gemini.google.com",
        "url_match":     "gemini.google.com",
        "input":         'rich-textarea div[contenteditable="true"]',
        "stop_btn":      'button[aria-label="Stop response"]',
        "response_sel":  "model-response .markdown",
    },
    "perplexity": {
        "url":           "https://www.perplexity.ai",
        "url_match":     "perplexity.ai",
        "input":         'textarea[placeholder]',
        "stop_btn":      None,
        "response_sel":  ".prose",
    },
    "grok": {
        "url":           "https://grok.com",
        "url_match":     "grok.com",
        "input":         'textarea[placeholder]',
        "stop_btn":      None,
        "response_sel":  ".message-content",
    },
    "copilot": {
        "url":           "https://copilot.microsoft.com",
        "url_match":     "copilot.microsoft.com",
        "input":         'textarea[placeholder]',
        "stop_btn":      None,
        "response_sel":  ".response-message",
    },
}


def site_for_agent(agent_name: str) -> Optional[dict]:
    """Match an agent name to a known site config by substring."""
    key_name = agent_name.lower().replace("_", "").replace(" ", "").replace("-", "")
    for key, cfg in SITES.items():
        if key in key_name:
            return {"key": key, **cfg}
    return None


def _type_and_submit(page, site: dict, message: str) -> None:
    """Type message into the chat input and press Enter."""
    sel = site["input"]
    try:
        page.wait_for_selector(sel, timeout=8000)
    except Exception:
        raise RuntimeError(
            f"Input box not found ({sel}). "
            "Is the page loaded and are you logged in?"
        )
    el = page.locator(sel).last
    el.click()
    el.press("Control+a")
    el.press("Delete")
    time.sleep(0.15)
    el.type(message, delay=20)
    time.sleep(0.2)
    el.press("Enter")


def _wait_and_read(page, site: dict, timeout: int) -> str:
    """Wait for generation to finish, return the last response text."""
    stop_sel = site.get("stop_btn")
    if stop_sel:
        try:
            page.wait_for_selector(stop_sel, timeout=10_000)
        except Exception:
            time.sleep(2)
        try:
            page.wait_for_selector(stop_sel, state="hidden", timeout=timeout * 1000)
        except Exception:
            pass  # Timed out — grab whatever is there
    else:
        time.sleep(max(timeout // 4, 8))

    resp_sel = site["response_sel"]
    elements = page.locator(resp_sel).all()
    if not elements:
        return "[No response text found — check the browser window]"
    return elements[-1].inner_text().strip()


class BrowserWorker:
    """
    Owns a single Playwright persistent browser context in a dedicated thread.
    The thread is the only one that ever touches Playwright objects.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._cmd_q     = queue.Queue()
        self._res_q     = queue.Queue()
        self._thread    = threading.Thread(target=self._run, daemon=True,
                                           name=f"browser-{agent_name}")
        self._thread.start()
        # Block until browser is open (or error)
        status, val = self._res_q.get(timeout=90)
        if status == "error":
            raise RuntimeError(val)

    # ── Internal thread ────────────────────────────────────────────────────────

    def _run(self):
        from playwright.sync_api import sync_playwright

        profile = PROFILES_DIR / self.agent_name
        profile.mkdir(parents=True, exist_ok=True)
        site = site_for_agent(self.agent_name)

        try:
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    str(profile),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if site and site["url_match"] not in page.url:
                    page.goto(site["url"])
                self._res_q.put(("ok", None))  # ready

                while True:
                    cmd = self._cmd_q.get()
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
                        elif action == "inject":
                            # Send cross-agent context without waiting for a full reply
                            msg, timeout = payload
                            _type_and_submit(page, site, msg)
                            reply = _wait_and_read(page, site, timeout)
                            self._res_q.put(("ok", reply))
                    except Exception as e:
                        self._res_q.put(("error", str(e)))
        except Exception as e:
            self._res_q.put(("error", str(e)))

    # ── Public API (called from Streamlit thread) ──────────────────────────────

    def send_message(self, message: str, timeout: int = 90) -> str:
        """Send a message, block until the AI finishes responding."""
        self._cmd_q.put(("send", (message, timeout)))
        status, val = self._res_q.get(timeout=timeout + 15)
        if status == "error":
            raise RuntimeError(val)
        return val

    def status(self) -> dict:
        """Get current page URL and title (non-blocking-ish)."""
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
