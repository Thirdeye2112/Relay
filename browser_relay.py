"""
browser_relay.py — Playwright browser automation for Relay.

One shared browser context (one window), one tab per agent.
All Playwright calls happen on a single dedicated thread.
"""
import queue
import threading
import time
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).parent / "browser_profiles"
SHARED_PROFILE = PROFILES_DIR / "_shared"

SITES: dict[str, dict] = {
    "chatgpt": {
        "url":          "https://chatgpt.com",
        "url_match":    "chatgpt.com",
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
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".message-content", "[class*='response']"],
    },
    "copilot": {
        "url":          "https://copilot.microsoft.com",
        "url_match":    "copilot.microsoft.com",
        "input":        ['textarea[placeholder]', 'div[contenteditable="true"]'],
        "stop_btn":     None,
        "send_btn":     None,
        "response_sel": [".response-message", "[class*='response']"],
    },
}


def site_for_agent(agent_name: str) -> Optional[dict]:
    key_name = agent_name.lower().replace("_", "").replace(" ", "").replace("-", "")
    for key, cfg in SITES.items():
        if key in key_name:
            return {"key": key, **cfg}
    return None


# ── Page helpers ───────────────────────────────────────────────────────────────

def _resolve_selector(page, selectors, timeout: int = 8000):
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
    loc = _resolve_selector(page, site["input"])
    if loc is None:
        raise RuntimeError(
            f"Input box not found (tried: {site['input']}). "
            "Is the page loaded and are you logged in?"
        )
    el = loc.last
    el.click()
    time.sleep(0.1)
    el.press("Control+a")
    time.sleep(0.05)
    el.press("Delete")
    time.sleep(0.1)
    el.type(message, delay=15)
    time.sleep(0.2)

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
    stop_sel = site.get("stop_btn")
    if stop_sel:
        appeared = False
        for _ in range(20):
            try:
                page.wait_for_selector(stop_sel, timeout=1000)
                appeared = True
                break
            except Exception:
                time.sleep(0.5)
        if appeared:
            try:
                page.wait_for_selector(stop_sel, state="hidden", timeout=timeout * 1000)
            except Exception:
                pass
        else:
            time.sleep(3)
    else:
        _wait_stable(page, site, timeout)
    return _read_last_response(page, site)


def _wait_stable(page, site: dict, timeout: int) -> None:
    deadline = time.time() + timeout
    prev, stable_count = "", 0
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


# ── BrowserManager — one window, tabs per agent ────────────────────────────────

class BrowserManager:
    """
    Single Playwright persistent context (one browser window).
    Each agent gets its own tab. All Playwright calls stay on one thread.
    """

    def __init__(self):
        self._cmd_q  = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="browser-manager")
        self._pages: dict[str, object] = {}
        self._thread.start()
        # Wait for browser to open
        try:
            status, val = self._cmd_q.join_result = None, None
            init_q = queue.Queue()
            self._init_q = init_q
            status, val = init_q.get(timeout=60)
        except queue.Empty:
            raise RuntimeError(
                "Browser did not open within 60 s. "
                "Run: python -m playwright install chromium"
            )
        if status == "error":
            raise RuntimeError(val)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            SHARED_PROFILE.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    str(SHARED_PROFILE),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                # Signal ready
                self._init_q.put(("ok", None))

                while True:
                    try:
                        cmd = self._cmd_q.get(timeout=1)
                    except queue.Empty:
                        # Heartbeat — check context still alive
                        try:
                            _ = ctx.pages
                        except Exception:
                            return
                        continue

                    if cmd is None:
                        ctx.close()
                        break

                    action, agent, payload, res_q = cmd
                    try:
                        if action == "open":
                            site = site_for_agent(agent)
                            # Reuse existing tab if the agent already has one open
                            page = self._pages.get(agent)
                            if page is None or page.is_closed():
                                page = ctx.new_page()
                                self._pages[agent] = page
                            if site and site["url_match"] not in page.url:
                                page.goto(site["url"], wait_until="domcontentloaded",
                                          timeout=30_000)
                            # Bring tab to front
                            page.bring_to_front()
                            res_q.put(("ok", None))

                        elif action == "send":
                            page = self._pages.get(agent)
                            if page is None or page.is_closed():
                                raise RuntimeError(f"No open tab for '{agent}'")
                            site = site_for_agent(agent)
                            if site is None:
                                raise RuntimeError(f"Unknown site for agent '{agent}'")
                            msg, timeout = payload
                            page.bring_to_front()
                            _type_and_submit(page, site, msg)
                            reply = _wait_and_read(page, site, timeout)
                            res_q.put(("ok", reply))

                        elif action == "close":
                            page = self._pages.pop(agent, None)
                            if page and not page.is_closed():
                                page.close()
                            res_q.put(("ok", None))

                        elif action == "status":
                            page = self._pages.get(agent)
                            if page and not page.is_closed():
                                res_q.put(("ok", {"url": page.url, "title": page.title()}))
                            else:
                                res_q.put(("ok", {}))

                        elif action == "list":
                            res_q.put(("ok", list(self._pages.keys())))

                    except Exception as e:
                        res_q.put(("error", str(e)))

        except Exception as e:
            try:
                self._init_q.put(("error", str(e)))
            except Exception:
                pass

    def _call(self, action: str, agent: str, payload=None, timeout: int = 30):
        res_q = queue.Queue()
        self._cmd_q.put((action, agent, payload, res_q))
        try:
            status, val = res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"Browser manager timed out on '{action}' for '{agent}'")
        if status == "error":
            raise RuntimeError(val)
        return val

    def open(self, agent_name: str) -> None:
        """Open a new tab for this agent and navigate to its site."""
        self._call("open", agent_name, timeout=40)

    def send_message(self, agent_name: str, message: str, timeout: int = 120) -> str:
        """Send a message in the agent's tab, wait for response."""
        return self._call("send", agent_name, (message, timeout), timeout=timeout + 30)

    def close_agent(self, agent_name: str) -> None:
        """Close this agent's tab."""
        self._call("close", agent_name, timeout=10)

    def status(self, agent_name: str) -> dict:
        """Return current URL + title for the agent's tab."""
        try:
            return self._call("status", agent_name, timeout=5) or {}
        except Exception:
            return {}

    def has_agent(self, agent_name: str) -> bool:
        """True if this agent has an open, non-closed tab."""
        try:
            s = self.status(agent_name)
            return bool(s)
        except Exception:
            return False

    def close(self) -> None:
        """Shut down the entire browser."""
        self._cmd_q.put(None)
        self._thread.join(timeout=10)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
