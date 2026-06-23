"""
browser_relay.py — Playwright browser automation for Relay.

One BrowserManager owns a single Playwright instance on one dedicated thread.
Each agent gets its own persistent profile (separate login) and its own window.
All Playwright calls are routed through the manager thread — never cross-thread.
"""
import os
import queue
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).parent / "browser_profiles"

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


def profile_exists(agent_name: str) -> bool:
    """True if this agent has a saved browser profile (i.e. has logged in before)."""
    p = PROFILES_DIR / agent_name
    return p.exists() and any(p.iterdir())


def reset_profile(agent_name: str) -> None:
    """Delete the agent's saved profile so it gets a fresh login next launch."""
    p = PROFILES_DIR / agent_name
    if not p.exists():
        return
    # Windows holds file locks briefly after Chrome closes — retry a few times
    def _force_remove(func, path, _):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    for attempt in range(5):
        try:
            shutil.rmtree(p, onerror=_force_remove)
            return
        except PermissionError:
            time.sleep(1)
    # Last attempt — remove what we can, leave locked files
    shutil.rmtree(p, ignore_errors=True)


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


def _kill_chrome_using_profile(profile_dir: str) -> None:
    """Kill any Chrome/Chromium process holding a lock on this profile directory."""
    try:
        subprocess.run([
            "powershell", "-NoProfile", "-Command",
            f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            f"Where-Object {{$_.CommandLine -like '*{profile_dir}*'}} | "
            f"ForEach-Object {{Stop-Process -Id $_.ProcessId -Force "
            f"-ErrorAction SilentlyContinue}}",
        ], capture_output=True, timeout=8)
        time.sleep(0.8)  # give Windows time to release file handles
    except Exception:
        pass


# ── BrowserManager ─────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Manages one Playwright instance on a single dedicated thread.
    Each agent gets its own persistent context (profile) and window,
    so logins are completely independent per agent.
    """

    def __init__(self):
        self._cmd_q    = queue.Queue()
        self._ready_q  = queue.Queue()
        self._contexts: dict[str, object] = {}
        self._pages:    dict[str, object] = {}
        self._thread   = threading.Thread(
            target=self._run, daemon=True, name="browser-manager"
        )
        self._thread.start()
        try:
            status, val = self._ready_q.get(timeout=30)
        except queue.Empty:
            raise RuntimeError(
                "Browser manager failed to start. "
                "Run: python -m playwright install chromium"
            )
        if status == "error":
            raise RuntimeError(val)

    # ── Thread ────────────────────────────────────────────────────────────────

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                self._pw = pw
                self._ready_q.put(("ok", None))

                while True:
                    try:
                        cmd = self._cmd_q.get(timeout=1)
                    except queue.Empty:
                        self._heartbeat()
                        continue

                    if cmd is None:
                        self._close_all()
                        break

                    action, agent, payload, res_q = cmd
                    try:
                        if   action == "open":   self._do_open(agent, res_q)
                        elif action == "send":   self._do_send(agent, payload, res_q)
                        elif action == "close":  self._do_close(agent, res_q)
                        elif action == "status": self._do_status(agent, res_q)
                        elif action == "list":   res_q.put(("ok", list(self._pages.keys())))
                    except Exception as e:
                        res_q.put(("error", str(e)))

        except Exception as e:
            try:
                self._ready_q.put(("error", str(e)))
            except Exception:
                pass

    def _heartbeat(self):
        dead = []
        for name, ctx in self._contexts.items():
            try:
                _ = ctx.pages
            except Exception:
                dead.append(name)
        for name in dead:
            self._contexts.pop(name, None)
            self._pages.pop(name, None)

    def _close_all(self):
        for ctx in list(self._contexts.values()):
            try:
                ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._pages.clear()

    def _do_open(self, agent: str, res_q):
        # Close stale Playwright context for this agent if present
        if agent in self._contexts:
            try:
                self._contexts[agent].pages  # probe
            except Exception:
                self._contexts.pop(agent, None)
                self._pages.pop(agent, None)

        if agent not in self._contexts:
            profile = PROFILES_DIR / agent
            profile.mkdir(parents=True, exist_ok=True)

            # Kill any Chrome process still holding this profile (exit code 21 guard)
            _kill_chrome_using_profile(str(profile))

            ctx = self._pw.chromium.launch_persistent_context(
                str(profile),
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--force-device-scale-factor=1",
                ],
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            self._contexts[agent] = ctx
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self._pages[agent] = page

            # Navigate to blank first so any saved-session page stops loading.
            # This guarantees our CDP command lands before any site JS runs.
            try:
                page.goto("about:blank", wait_until="commit", timeout=5_000)
            except Exception:
                pass

            # Now send setSkipAllPauses — page is idle, no race condition.
            try:
                cdp = ctx.new_cdp_session(page)
                cdp.send("Debugger.enable")
                cdp.send("Debugger.setSkipAllPauses", {"skip": True})
            except Exception:
                pass

            site = site_for_agent(agent)
            if site:
                page.goto(site["url"], wait_until="domcontentloaded", timeout=30_000)

        res_q.put(("ok", None))

    def _do_send(self, agent: str, payload, res_q):
        page = self._pages.get(agent)
        if page is None or page.is_closed():
            raise RuntimeError(f"No open window for '{agent}' — launch it first")
        site = site_for_agent(agent)
        if site is None:
            raise RuntimeError(f"Unknown site for agent '{agent}'")
        msg, timeout = payload
        page.bring_to_front()
        _type_and_submit(page, site, msg)
        reply = _wait_and_read(page, site, timeout)
        res_q.put(("ok", reply))

    def _do_close(self, agent: str, res_q):
        ctx = self._contexts.pop(agent, None)
        self._pages.pop(agent, None)
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        res_q.put(("ok", None))

    def _do_status(self, agent: str, res_q):
        page = self._pages.get(agent)
        if page and not page.is_closed():
            try:
                res_q.put(("ok", {"url": page.url, "title": page.title()}))
                return
            except Exception:
                pass
        res_q.put(("ok", {}))

    # ── Public API ────────────────────────────────────────────────────────────

    def _call(self, action: str, agent: str, payload=None, timeout: int = 40):
        res_q = queue.Queue()
        self._cmd_q.put((action, agent, payload, res_q))
        try:
            status, val = res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"Timed out on '{action}' for '{agent}'")
        if status == "error":
            raise RuntimeError(val)
        return val

    def open(self, agent_name: str) -> None:
        self._call("open", agent_name, timeout=45)

    def send_message(self, agent_name: str, message: str, timeout: int = 120) -> str:
        return self._call("send", agent_name, (message, timeout), timeout=timeout + 30)

    def close_agent(self, agent_name: str) -> None:
        self._call("close", agent_name, timeout=10)

    def status(self, agent_name: str) -> dict:
        try:
            return self._call("status", agent_name, timeout=5) or {}
        except Exception:
            return {}

    def has_agent(self, agent_name: str) -> bool:
        try:
            return bool(self.status(agent_name))
        except Exception:
            return False

    def active_agents(self) -> list:
        try:
            return self._call("list", "", timeout=5)
        except Exception:
            return []

    def close(self) -> None:
        self._cmd_q.put(None)
        self._thread.join(timeout=10)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
