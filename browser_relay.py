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
from typing import Optional

CDP_URL = "http://localhost:9222"

SITES: dict[str, dict] = {
    "claude": {
        "url":          "https://claude.ai/new",
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
            'textarea[placeholder*="Ask"]',
            'textarea[placeholder]',
            'div[contenteditable="true"]',
        ],
        "stop_btn":     'button[aria-label="Stop"]',
        "send_btn":     'button[aria-label="Submit"]',
        "response_sel": [".prose", ".answer-text", "[class*='answer']"],
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
            f"Input box not found on {page.url}. "
            "Is the page fully loaded and are you logged in?"
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

    submitted = False
    send_sel = site.get("send_btn")
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
                        if   action == "scan":     self._do_scan(res_q)
                        elif action == "assign":   self._do_assign(agent, payload, res_q)
                        elif action == "open_tab": self._do_open_tab(agent, res_q)
                        elif action == "send":     self._do_send(agent, payload, res_q)
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

    def _do_open_tab(self, agent: str, res_q):
        site = site_for_agent(agent)
        if not site:
            res_q.put(("error", f"No site configured for '{agent}'"))
            return
        try:
            ctx = self._browser.contexts[0]
            page = ctx.new_page()
            page.goto(site["url"], wait_until="domcontentloaded", timeout=30_000)
            self._pages[agent] = page
            res_q.put(("ok", None))
        except Exception as e:
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
        _type_and_submit(page, site, msg)
        reply = _wait_and_read(page, site, timeout)
        res_q.put(("ok", reply))

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
