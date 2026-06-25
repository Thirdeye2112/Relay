#!/usr/bin/env python3
"""relay/app.py — Run: python -m streamlit run app.py"""

import os, sys, json, datetime, time, threading
from typing import Optional
import streamlit as st
from pathlib import Path

try:
    from relay_log import get_logger as _get_logger
    _log = _get_logger("relay.app")
except Exception:
    import logging
    _log = logging.getLogger("relay.app")

sys.path.insert(0, str(Path(__file__).parent))
from relay import (
    load_agents, load_session, load_meta, save_meta,
    append_message, Message, SESSIONS_DIR, AGENTS_FILE, AgentConfig,
)
try:
    import importlib.util as _ilu
    _br_path = Path(__file__).parent / "browser_relay.py"
    _br_spec = _ilu.spec_from_file_location("browser_relay", _br_path)
    _br_mod  = _ilu.module_from_spec(_br_spec)
    _br_spec.loader.exec_module(_br_mod)
    BrowserManager = _br_mod.BrowserManager
    site_for_agent  = _br_mod.site_for_agent
    classify_tab_url = _br_mod.classify_tab_url
    SITES          = _br_mod.SITES
    CDP_URL        = _br_mod.CDP_URL
    PLAYWRIGHT_AVAILABLE = True
    PLAYWRIGHT_ERROR = None
except Exception as _pw_err:
    PLAYWRIGHT_AVAILABLE = False
    PLAYWRIGHT_ERROR = str(_pw_err)
    _log.error(f"Playwright unavailable: {_pw_err}")
    def site_for_agent(name, site_key_override=""): return None  # noqa: E704
    def classify_tab_url(url): return "unsupported"  # noqa: E704
    SITES = {}
    CDP_URL = "http://localhost:9222"

try:
    from github_relay import (
        get_repo as _gh_get_repo, append_round as _gh_append_round,
        commit_async, html_url as _gh_html_url, raw_url as _gh_raw_url,
        topic_to_path,
    )
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Relay", page_icon="⚡", layout="wide")

KEYS_FILE = Path(__file__).parent / "keys.json"

def load_keys() -> dict:
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    return {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "",
            "GITHUB_TOKEN": "", "GITHUB_REPO": "relay-conversations"}

def save_keys(keys: dict) -> None:
    KEYS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")

def apply_keys(keys: dict) -> None:
    for k, v in keys.items():
        if v and isinstance(v, str):
            os.environ[k] = v

saved_keys = load_keys()
apply_keys(saved_keys)

# ── GitHub ────────────────────────────────────────────────────────────────────

@st.cache_resource
def _cached_repo(token: str, repo_name: str):
    return _gh_get_repo(token, repo_name)

def get_github_repo():
    if not GITHUB_AVAILABLE:
        return None
    token = saved_keys.get("GITHUB_TOKEN", "")
    repo  = saved_keys.get("GITHUB_REPO", "relay-conversations")
    if not token or not repo:
        return None
    try:
        return _cached_repo(token, repo)
    except Exception:
        return None

# ── Agents ────────────────────────────────────────────────────────────────────

agents_cfg = load_agents()

_EMOJIS = ["🔵", "🔴", "🟢", "🟡", "🟠", "🟣"]
def avatar(name: str) -> str:
    names = list(agents_cfg.keys())
    return _EMOJIS[names.index(name) % len(_EMOJIS)] if name in names else "⚪"

def _api_agent_active(cfg) -> bool:
    """True when the API key for this agent's provider is set."""
    key = "ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"
    return bool(os.environ.get(key, ""))

def agent_active(cfg, assigned: "set | None" = None) -> bool:
    """True when this agent is ready to receive messages.

    For browser agents pass the pre-fetched `assigned` set returned by
    get_manager().list_agents() to avoid a per-agent queue round-trip.
    """
    if getattr(cfg, "mode", "api") == "browser":
        if assigned is not None:
            return cfg.name in assigned
        m = get_manager()
        return bool(m and m.has_agent(cfg.name))
    return _api_agent_active(cfg)

def close_worker(name: str) -> None:
    m = get_manager()
    if m:
        m.unassign(name)

def active_agents() -> dict:
    """Return {name: cfg} for every agent currently ready to receive messages.

    For browser agents this is a single list_agents() call (one queue round-trip)
    instead of one has_agent() call per agent — critical for tab-switch latency
    since active_agents() is called unconditionally on every Streamlit rerun.
    """
    m        = get_manager()
    assigned = set(m.list_agents()) if m else set()
    return {
        n: c for n, c in agents_cfg.items()
        if agent_active(c, assigned)
    }

def save_agents(agents: dict) -> None:
    import yaml
    data = {"agents": {
        name: {
            "provider":      c.provider,
            "model":         c.model,
            "mode":          getattr(c, "mode", "api"),
            "system_prompt": c.system_prompt,
            **({"site_key": c.site_key} if getattr(c, "site_key", "") else {}),
        }
        for name, c in agents.items()
    }}
    with open(AGENTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Streaming ─────────────────────────────────────────────────────────────────

def _merge_consecutive_roles(msgs: list[dict]) -> list[dict]:
    """Collapse consecutive same-role entries into one.

    The cross-inject step appends a "user" message at the end of every round
    (the other agents' replies); the next round then appends ANOTHER "user"
    message (the new user input) before this agent ever gets to reply --
    two consecutive "user" entries in a row. Anthropic's Messages API
    requires strict user/assistant alternation and rejects this with a 400.
    Merging is a defensive normalization right at the API boundary rather
    than changing the round/storage logic, so it's robust regardless of
    which code path produced the consecutive entries.
    """
    merged: list[dict] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] = merged[-1]["content"] + "\n\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    return merged


def _stream_anthropic(model: str, messages: list[Message]):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        st.error("ANTHROPIC_API_KEY not set — add it in Settings."); st.stop()
    client   = anthropic.Anthropic(api_key=key)
    system   = next((m.content for m in messages if m.role == "system"), "")
    api_msgs = _merge_consecutive_roles(
        [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    )
    with client.messages.stream(model=model, max_tokens=2048, system=system, messages=api_msgs) as s:
        for chunk in s.text_stream:
            yield chunk

def _stream_openai(model: str, messages: list[Message]):
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        st.error("OPENAI_API_KEY not set — add it in Settings."); st.stop()
    stream = OpenAI(api_key=key).chat.completions.create(
        model=model,
        messages=_merge_consecutive_roles(
            [{"role": m.role, "content": m.content} for m in messages]
        ),
        max_tokens=2048, stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta

def stream_agent(cfg, messages: list[Message]):
    if cfg.provider == "anthropic":
        yield from _stream_anthropic(cfg.model, messages)
    elif cfg.provider == "openai":
        yield from _stream_openai(cfg.model, messages)

def _api_error(e: Exception) -> str:
    msg = str(e)
    if "credit balance" in msg.lower():
        return "Credit balance too low — top up at console.anthropic.com/billing (note: Claude.ai Plus ≠ API credits)"
    if "rate limit" in msg.lower():
        return "Rate limit — wait a moment and try again"
    if "401" in msg or "authentication" in msg.lower():
        return "Invalid API key — check Settings"
    return f"API error: {msg[:200]}"

# ── Browser manager ───────────────────────────────────────────────────────────

def get_manager() -> Optional["BrowserManager"]:
    m = st.session_state.get("browser_manager")
    return m if (m and m.alive) else None

def connect_browser() -> None:
    """Connect to the user's running Chrome and auto-assign tabs."""
    m = BrowserManager()
    st.session_state.browser_manager = m
    auto_assign_tabs(m)

def auto_assign_tabs(m: "BrowserManager") -> None:
    """Scan open tabs and assign each browser agent to the first matching tab.

    claude.ai/code is Claude Code's IDE web view, not the chat composer --
    "claude.ai" is a substring of that URL too, so a plain url_match check
    would happily hand a Claude Code tab to a chat-persona agent (e.g.
    claude_code_architect/claude_code_pragmatist are chat personas, not meant
    to drive the actual Claude Code product UI). classify_tab_url() tells
    chat tabs apart from /code tabs; auto-assign only ever matches a
    claude_code-classified tab to an agent that explicitly opted in via
    site_key == "claude_code" -- never inferred from the agent's name.
    """
    tabs = m.scan_tabs()
    used: set[int] = set()
    for name, cfg in agents_cfg.items():
        if getattr(cfg, "mode", "api") != "browser":
            continue
        if m.has_agent(name):
            continue
        site_key_override = getattr(cfg, "site_key", "")
        site = site_for_agent(name, site_key_override)
        if not site:
            continue
        wants_code = site["key"] == "claude_code"
        for i, tab in enumerate(tabs):
            if i in used:
                continue
            cls = classify_tab_url(tab["url"])
            if wants_code:
                if cls != "claude_code":
                    continue
            elif site["key"] == "claude_chat":
                if cls != "claude_chat":
                    continue
            elif site["url_match"] not in tab["url"]:
                continue
            try:
                m.assign_tab(name, i, site_key_override)
                used.add(i)
            except Exception:
                pass
            break

def find_tab_for_agent(m: "BrowserManager", agent_name: str, cfg: Optional["AgentConfig"] = None) -> tuple[str | None, str]:
    """Find an open tab matching this agent and assign it.

    Returns (tab_url_or_None, message). Classification-aware: a chat persona
    (no site_key override, or override == "claude_chat") only matches tabs
    classified "claude_chat"; a claude_code persona (site_key == "claude_code")
    only matches tabs classified "claude_code". Tabs already assigned to a
    DIFFERENT agent are skipped with a specific reason rather than silently
    stolen — one agent owns one tab is a hard rule, not a preference.
    """
    site_key_override = getattr(cfg, "site_key", "") if cfg else ""
    site = site_for_agent(agent_name, site_key_override)
    if not site:
        return None, f"No site configured for '{agent_name}'."

    wants_code = site["key"] == "claude_code"
    is_chat_persona = site["key"] == "claude_chat"
    tabs = m.scan_tabs()

    rejected_path_seen  = False  # saw a claude.ai/code tab while looking for a chat persona
    already_assigned_to = None
    for i, tab in enumerate(tabs):
        cls = classify_tab_url(tab["url"])
        if wants_code:
            if cls != "claude_code":
                continue
        elif is_chat_persona:
            if cls == "claude_code":
                rejected_path_seen = True
                continue
            if cls != "claude_chat":
                continue
        else:
            if site["url_match"] not in tab["url"]:
                continue
        marker = tab.get("marker", "")
        if marker and marker != agent_name:
            already_assigned_to = marker
            continue
        try:
            m.assign_tab(agent_name, i, site_key_override)
            return tab["url"], f"Assigned {agent_name.upper()} to tab: {tab['url'][:80]}"
        except Exception:
            continue

    if already_assigned_to:
        return None, f"Found Claude tab, but it is already assigned to {already_assigned_to.upper()}."
    if wants_code:
        return None, "No Claude Code tab found. Open claude.ai/code, or configure this agent as a normal Claude chat persona."
    if rejected_path_seen:
        return None, "Found claude.ai/code, but this agent is configured as a Claude chat persona, so the tab was rejected."
    if site["key"] in ("claude_chat",):
        return None, "No Claude chat tab found. Open a normal claude.ai chat tab, not claude.ai/code."
    return None, f"No open {site['url_match']} tab found — open it in Chrome first, then click Find Tab."

def disconnect_browser() -> None:
    m = st.session_state.pop("browser_manager", None)
    if m:
        m.close()

# ── Sessions ──────────────────────────────────────────────────────────────────

def all_sessions() -> list[Path]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(
        [s for s in SESSIONS_DIR.iterdir() if (s / "meta.json").exists()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )

def session_label(sid: str) -> str:
    m = load_meta(sid)
    if not m:
        return sid
    agents = "  ·  ".join(a.upper() for a in m.get("agents", []))
    topic  = m.get("topic", "")[:30]
    return f"{agents} — {topic}"

def delete_session(sid: str) -> None:
    import shutil
    p = SESSIONS_DIR / sid
    if p.exists():
        shutil.rmtree(p)

# ── File attachment helpers ───────────────────────────────────────────────────

_UPLOAD_TYPES = [
    # Text & markdown
    "txt", "md", "rst", "tex", "log", "diff", "patch",
    # Code — web
    "py", "js", "ts", "jsx", "tsx", "vue", "svelte", "html", "css",
    # Code — systems
    "go", "rs", "c", "cpp", "h", "cs", "java", "kt", "swift",
    # Code — scripting
    "rb", "php", "sh", "bash", "ps1", "bat", "r", "m", "lua", "pl",
    # Code — functional / other
    "ex", "exs", "fs", "fsx", "scala", "clj", "dart", "elm", "zig", "nim",
    # Data & config
    "json", "jsonl", "ndjson", "yaml", "yml", "toml", "csv", "tsv",
    "xml", "sql", "ini", "cfg", "conf", "env", "properties",
    # Documents
    "pdf", "docx", "doc", "xlsx", "xls", "odt", "rtf",
    # Notebook
    "ipynb",
    # Images (SVG is text; raster images get a helpful decline message)
    "svg", "png", "jpg", "jpeg", "gif", "webp",
]


def _read_uploaded_file(uploaded) -> str:
    """Extract text content from an uploaded file, with graceful fallbacks."""
    import io
    content = uploaded.read()
    ext  = (uploaded.name or "").rsplit(".", 1)[-1].lower() if "." in (uploaded.name or "") else ""
    mime = uploaded.type or ""

    if ext == "pdf" or "pdf" in mime:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            return "[PDF attached — run: pip install pypdf]"

    if ext in ("docx", "doc"):
        try:
            import docx as _docx
            doc = _docx.Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs if p.text)
        except ImportError:
            return "[DOCX attached — run: pip install python-docx]"
        except Exception as e:
            return f"[Could not read DOCX: {e}]"

    if ext in ("xlsx", "xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            lines = []
            for sheet in wb.worksheets:
                lines.append(f"--- Sheet: {sheet.title} ---")
                for row in sheet.iter_rows(values_only=True):
                    lines.append("\t".join("" if v is None else str(v) for v in row))
            return "\n".join(lines)
        except ImportError:
            return "[XLSX attached — run: pip install openpyxl]"
        except Exception as e:
            return f"[Could not read XLSX: {e}]"

    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"):
        return f"[Image: {uploaded.name} — image data cannot be sent as text to browser agents]"

    # All other types: decode as text
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return content.decode("latin-1")
        except Exception:
            return f"[Binary file: {uploaded.name} — could not extract text content]"


# ── Display log ───────────────────────────────────────────────────────────────

def display_path(sid: str) -> Path:
    return SESSIONS_DIR / sid / "display.jsonl"

def append_display(sid: str, event: dict) -> None:
    p = display_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

DEBUG_DIR = Path(__file__).parent / "debug"
CONTEXT_DIR = Path(__file__).parent / "context_files"

def _dump_payloads_to_disk(payloads: dict) -> None:
    """Write the exact outgoing string for every browser agent this round to
    disk (debug/round_payload_*.txt) -- ground truth for whether cross-
    injection actually produced real content, captured before send_batch
    touches any tab (independent of anything the transport does afterward).
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        for name, payload in payloads.items():
            (DEBUG_DIR / f"round_payload_{name}_{stamp}.txt").write_text(payload, encoding="utf-8")
    except Exception:
        pass

def render_display(sid: str) -> None:
    p = display_path(sid)
    if not p.exists():
        return

    events = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # Group events by round_id (0 = pre-session / legacy events)
    from collections import defaultdict
    by_round: dict[int, list] = defaultdict(list)
    for ev in events:
        by_round[ev.get("round_id") or 0].append(ev)

    for rid in sorted(by_round.keys()):
        revents = by_round[rid]

        # Round header
        marker = next((e["content"] for e in revents if e["type"] == "auto_round_marker"), None)
        if rid == 0:
            pass  # pre-session events — no header
        elif marker:
            st.markdown(f"---\n#### {marker}")
        else:
            st.markdown(f"---\n#### Round {rid}")

        # Transport stats — the real success metric is confirmed rate, not
        # "no failures": a round passing only via semantic_unconfirmed is
        # limping, not fixed. Shown unconditionally (not buried in the
        # collapsed Debug expander) so a provider that's quietly degrading
        # is easy to spot round over round, not just when something errors.
        stats_ev = next((e for e in revents if e["type"] == "transport_stats"), None)
        if stats_ev:
            s = stats_ev["stats"]
            st.caption(
                f"📊 confirmed: {s.get('confirmed', 0)} · "
                f"semantic_unconfirmed: {s.get('semantic_unconfirmed', 0)} · "
                f"quarantined: {s.get('quarantined', 0)} · "
                f"transport_failure: {s.get('transport_failure', 0)}"
            )

        # Collect debug items: payloads + failure notices + lineage warnings
        debug_items       = []
        failures          = []
        envelope_warnings = []  # [(agent, warning_str), ...]
        diagnostics       = []  # [diagnostics dict, ...] — speed/quality per agent per round

        for ev in revents:
            etype = ev["type"]
            if etype == "user":
                with st.chat_message("user", avatar="🧑"):
                    st.markdown(ev["content"])
            elif etype == "reply":
                content = ev["content"]
                env_e   = ev.get("envelope", {})
                if _is_relay_failure(content):
                    # Pass full envelope so debug expander can show quarantine detail
                    failures.append((ev["agent"], content, env_e))
                    # Failures rendered below in debug expander — not in chat stream
                else:
                    retry_tag = " *(retry)*" if ev.get("retry") else ""
                    with st.chat_message(ev["agent"], avatar=avatar(ev["agent"])):
                        st.markdown(f"**{ev['agent'].upper()}**{retry_tag}\n\n{content}")
                # Collect any lineage warnings from the envelope
                for w in env_e.get("warnings", []):
                    envelope_warnings.append((ev["agent"], w))
                diag = env_e.get("diagnostics")
                if diag:
                    diagnostics.append(diag)
            elif etype == "synthesis":
                with st.chat_message("synthesis", avatar="🔮"):
                    st.markdown(f"**SYNTHESIS**\n\n{ev['content']}")
            elif etype == "auto_round_marker":
                pass  # already shown in header
            elif etype == "payload_dump":
                debug_items.append(ev)

        # Debug / Recovery panel — shown only when there is something to show
        if failures or debug_items or envelope_warnings or diagnostics:
            label = f"🔍 Debug / Recovery — {len(failures)} failure(s)" if failures else "🔍 Debug"
            with st.expander(label, expanded=bool(failures)):
                for ag, content, env_f in failures:
                    st.error(f"⚠️ System Failure Notice — **{ag.upper()}**: {content}")
                    # Quarantine detail: structured reason + raw captured text
                    if env_f.get("quarantined"):
                        reason = env_f.get("discard_reason") or "unknown"
                        st.caption(f"Quarantine reason: {reason}")
                        raw_cap = env_f.get("raw_payload") or ""
                        if raw_cap and raw_cap != content:
                            with st.expander(f"Raw capture — {ag.upper()}", expanded=False):
                                st.code(raw_cap[:1000], language=None)
                    # Capture method if degraded
                    cm = env_f.get("capture_method")
                    if cm and cm != "semantic":
                        st.caption(f"Capture method: {cm}")
                if envelope_warnings:
                    for ag, w in envelope_warnings:
                        st.caption(f"⚡ Lineage — **{ag.upper()}**: {w}")
                for ev in debug_items:
                    st.caption("Payloads transmitted this round:")
                    for name, payload in ev.get("payloads", {}).items():
                        st.caption(name.upper())
                        st.code(payload, language=None)
                if diagnostics:
                    st.caption("⏱️ Speed diagnostics — where the time went, per agent:")
                    rows = []
                    for d in diagnostics:
                        rows.append({
                            "agent":        d.get("agent", "").upper(),
                            "tab":          d.get("tab_classification") or "—",
                            "prompt_chars": d.get("prompt_chars", 0),
                            "construct_ms": d.get("construction_ms"),
                            "insert_ms":    d.get("insertion_ms"),
                            "send_ms":      d.get("send_verify_ms"),
                            "wait_ms":      d.get("wait_response_ms"),
                            "capture":      d.get("capture_method") or "—",
                            "state":        d.get("reply_state") or "—",
                            "fail_stage":   d.get("failure_stage") or "—",
                            "context":      d.get("context_mode") or "inline",
                        })
                    st.table(rows)

# ── Transcript export ─────────────────────────────────────────────────────────

def _generate_clean_transcript(sid: str) -> str:
    """Markdown export: round labels, user prompts, valid agent replies only."""
    meta   = load_meta(sid)
    p      = display_path(sid)
    agents = meta.get("agents", [])
    lines  = [
        f"# {meta.get('topic', 'Relay Conversation')}",
        f"Agents: {', '.join(a.upper() for a in agents)}",
        f"Created: {meta.get('created', '')}",
        "",
        "---",
    ]
    if not p.exists():
        return "\n".join(lines + ["", "No transcript yet."])

    from collections import defaultdict
    events   = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_round: dict = defaultdict(list)
    for ev in events:
        by_round[ev.get("round_id") or 0].append(ev)

    for rid in sorted(by_round.keys()):
        revents = by_round[rid]
        marker  = next((e["content"] for e in revents if e["type"] == "auto_round_marker"), None)
        if rid == 0 and not marker:
            pass
        elif marker:
            lines += ["", f"## {marker}", ""]
        else:
            lines += ["", f"## Round {rid}", ""]

        for ev in revents:
            etype   = ev["type"]
            content = ev.get("content", "")
            if etype == "user":
                lines += [f"**You:** {content}", ""]
            elif etype == "reply" and not _is_relay_failure(content) and not _is_bad_capture(content):
                retry_tag = " *(retry)*" if ev.get("retry") else ""
                lines += [f"**{ev['agent'].upper()}**{retry_tag}:", content, ""]
            elif etype == "synthesis":
                lines += ["**SYNTHESIS:**", content, ""]

    return "\n".join(lines)


def _generate_debug_transcript(sid: str) -> str:
    """Markdown export including full lineage envelope metadata."""
    meta  = load_meta(sid)
    p     = display_path(sid)
    lines = [
        f"# Debug Transcript — {sid}",
        f"Topic: {meta.get('topic', '')}",
        f"Created: {meta.get('created', '')}",
        "",
    ]
    if not p.exists():
        return "\n".join(lines + ["No events yet."])

    events = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ev in events:
        etype   = ev.get("type", "?")
        rid     = ev.get("round_id", "—")
        content = ev.get("content", "")
        if etype == "reply":
            env   = ev.get("envelope", {})
            retry = " [RETRY]" if ev.get("retry") else ""
            lines += [
                f"### [{etype.upper()}] {ev.get('agent','?').upper()} — Round {rid}{retry}",
                f"- validated: {env.get('validated', '?')}",
                f"- capture_method: {env.get('capture_method', '?')}",
                f"- pre_count: {env.get('pre_response_count', '?')}",
                f"- new_index: {env.get('new_response_index', '?')}",
                f"- warnings: {env.get('warnings', [])}",
                f"- event_id: {env.get('event_id', '?')}",
                f"- failure: {_is_relay_failure(content)}",
                "",
                "```",
                content[:600] + ("…" if len(content) > 600 else ""),
                "```",
                "",
            ]
        elif etype == "payload_dump":
            lines.append(f"### [PAYLOAD_DUMP] Round {rid}")
            for name, pl in ev.get("payloads", {}).items():
                lines += [f"**{name.upper()}** ({len(pl):,} chars)", "```", pl[:400] + "…", "```", ""]
        elif etype == "auto_round_marker":
            lines += [f"### [ROUND MARKER] {content}", ""]
        elif etype == "user":
            lines += [f"### [USER] Round {rid}", content[:400], ""]
        elif etype == "synthesis":
            lines += [f"### [SYNTHESIS] Round {rid}", content[:400], ""]
        elif etype == "transport_stats":
            s = ev.get("stats", {})
            lines += [f"### [TRANSPORT STATS] Round {rid}",
                     f"- confirmed: {s.get('confirmed', 0)}",
                     f"- semantic_unconfirmed: {s.get('semantic_unconfirmed', 0)}",
                     f"- quarantined: {s.get('quarantined', 0)}",
                     f"- transport_failure: {s.get('transport_failure', 0)}", ""]

    return "\n".join(lines)


# ── Synthesizer ───────────────────────────────────────────────────────────────

def _build_synthesis_prompt(user_msg: str, replies: dict) -> str:
    lines = [f"The group was asked:\n> {user_msg}\n\nHere is what each participant said:\n"]
    for name, reply in replies.items():
        lines.append(f"**{name.upper()}:**\n{reply}\n")
    lines.append(
        "\nYour job: synthesize this exchange in 2–4 paragraphs.\n"
        "1. Identify the core points of agreement.\n"
        "2. Identify the core points of disagreement or tension.\n"
        "3. Surface any angles, risks, or insights that were missed or underweighted.\n"
        "4. End with a single sentence verdict or recommendation.\n"
        "Be direct. Do not just summarize — synthesize."
    )
    return "\n".join(lines)

def run_synthesis(sid: str, user_msg: str, replies: dict, round_id: int | None = None,
                   mgr: Optional["BrowserManager"] = None) -> None:
    """Synthesize the round.

    Prefers reusing one of THIS round's successfully-replying browser agents
    (no separate API key, no extra cost, consistent with every other agent
    in Relay) -- falls back to a direct Anthropic API call only if no
    browser agent is available (e.g. an all-API-mode session) and a funded
    ANTHROPIC_API_KEY is set. The old default (always call Anthropic
    directly) required a real billed API key -- a different account/cost
    from a Claude.ai subscription -- which is exactly what broke.
    """
    if not replies:
        return
    prompt = _build_synthesis_prompt(user_msg, replies)

    browser_synth_agent = next(
        (n for n in replies if mgr and mgr.has_agent(n)), None
    )

    if browser_synth_agent:
        synth_prompt = (
            "Set aside your own position for a moment. Acting as a neutral "
            "synthesizer, not as a participant, synthesize the discussion above:\n\n"
            + prompt
        )
        with st.chat_message("synthesis", avatar="🔮"):
            st.markdown(f"**SYNTHESIS** (via {browser_synth_agent.upper()})")
            with st.spinner(f"Synthesizing via {browser_synth_agent.upper()}…"):
                try:
                    env = mgr.send_message(browser_synth_agent, synth_prompt)
                except Exception as e:
                    _log.error(f"[sid={sid}, round={round_id}] Synthesis transport error via {browser_synth_agent} — {e}", exc_info=True)
                    st.error(f"Synthesis failed: {e}")
                    return
            synthesis, validated, raw = _extract_reply(env)
            # Same stale-DOM quarantine gate run_round applies to every batch
            # reply — send_message now returns the same lineage envelope, so
            # synthesis gets the same grace-retry/semantic_unconfirmed
            # protection instead of trusting whatever text came back.
            if (not validated and not _is_relay_failure(synthesis)
                    and raw.get("capture_method") != "semantic_unconfirmed"):
                pre, nw = raw.get("pre_response_count", "?"), raw.get("new_response_index", "?")
                synthesis = f"[Error: Stale DOM read — element count {pre}→{nw}, new response not confirmed]"
            if _is_relay_failure(synthesis):
                _log.warning(f"[sid={sid}, round={round_id}] Synthesis relay failure via {browser_synth_agent} — {synthesis[:120]}")
                st.error(f"Synthesis failed: {synthesis}")
                return
            st.markdown(synthesis)
        append_display(sid, {"type": "synthesis", "content": synthesis, "round_id": round_id})
        return

    # Fallback: direct Anthropic API call -- only reachable when no browser
    # agent produced a reply this round.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return  # silently skip -- no browser agent available and no API key set

    synth_msgs = [
        Message("system", "You are a neutral synthesizer. You do not have a side. You clarify."),
        Message("user", prompt),
    ]
    with st.chat_message("synthesis", avatar="🔮"):
        st.markdown("**SYNTHESIS**")
        try:
            synthesis = st.write_stream(
                _stream_anthropic("claude-haiku-4-5-20251001", synth_msgs)
            )
        except Exception as e:
            _log.error(f"[sid={sid}, round={round_id}] Synthesis API error — {e}", exc_info=True)
            st.error(_api_error(e))
            return

    append_display(sid, {"type": "synthesis", "content": synthesis, "round_id": round_id})

# ── Group round ───────────────────────────────────────────────────────────────

_MAX_CROSS_CHARS        = 1500   # per-agent cap (normal sessions)
_CROSS_CONTEXT_THRESHOLD = 16_000  # total chars across all agents before tightening
_CROSS_CONTEXT_TIGHT_CAP = 600    # per-agent cap when over threshold

# File mode (opt-in, off by default — see "📎 File mode" control panel toggle):
# above this many chars, the outgoing message is written to disk and an
# attach attempt is made instead of pasting the full text inline. Existing
# cross-context caps above already keep most rounds well under this; this
# only kicks in for rounds that are still large after those caps (many
# agents each near _MAX_CROSS_CHARS, plus the round-1 system prompt, etc.).
_FILE_ATTACH_THRESHOLD_CHARS = 3000

# Sentinels Relay itself produces when a browser agent's round failed --
# a typed-but-never-sent message, a timeout, a missing tab, etc. These must
# never be treated as a real reply: broadcasting "[Error: ...]" to every
# other agent as if it were the failed agent's actual position corrupts
# every subsequent round's context for the whole panel from one tab hiccup,
# and counting it as "this agent has replied" would also wrongly skip
# sending it the round-1 system prompt next time.
_FAILURE_PREFIXES = ("[Error:", "[Timed out]", "[No response captured", "[Not sent")

def _is_relay_failure(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith(_FAILURE_PREFIXES)


# Payload quality guard — catches bad captures that don't start with a known
# sentinel but are still clearly not real agent speech.
_PAYLOAD_ECHO_PHRASES = [
    "HOW THE RELAY WORKS",
    "=== New relay session starting ===",
    "The other panelists said:",
    "=== INSTRUCTIONS ===",
]

def _is_bad_capture(payload: str) -> bool:
    """True for empty, known-failure, or prompt-echo payloads.

    Echo phrases are only checked in the first 400 characters.  Sites like
    Perplexity capture a text diff that can include the search query at the
    top (which echoes relay instructions) followed by the real answer.  A
    short preamble check catches genuine bad captures (nothing but the relay
    prompt was returned) without discarding long valid responses that happen
    to quote an instruction phrase deep in the answer.
    """
    t = (payload or "").strip()
    if not t:
        return True
    if t.startswith(_FAILURE_PREFIXES):
        return True
    preamble = t[:400]
    return any(phrase in preamble for phrase in _PAYLOAD_ECHO_PHRASES)

def _validate_payload_quality(payload: str) -> "tuple[bool, str | None]":
    """Returns (is_valid, failure_reason).  Conservative — only rejects obvious bad captures."""
    if _is_bad_capture(payload):
        snippet = (payload or "").strip()[:100]
        return False, f"Bad capture marker or prompt echo: {snippet!r}"
    return True, None


def _semantic_unconfirmed_passes_strict_checks(
    payload: str, outgoing_payload: str, sid: str, name: str
) -> "tuple[bool, str | None]":
    """Extra gate specifically for capture_method == "semantic_unconfirmed".

    A bare "non-empty, non-sentinel" check (already applied before this
    capture_method is even assigned) can't distinguish a genuine new reply
    from two specific failure shapes that also look "real" by that check
    alone: an accidental echo of what was just SENT this round, or a stale
    DOM read that's just the agent's own unchanged previous turn. Keeping
    semantic_unconfirmed a rare, trustworthy escape hatch instead of a
    second normal path depends on ruling both out before accepting it.
    """
    stripped = (payload or "").strip()

    # Echo check: a long contiguous match against this round's own outgoing
    # prompt is not a coincidence -- a short shared phrase is normal and
    # expected (agents often quote each other), a long one is the page
    # reading back what was typed into it rather than a generated reply.
    if outgoing_payload:
        check_len = min(80, len(stripped))
        if check_len >= 20 and stripped[:check_len] in outgoing_payload:
            return False, "echo — captured text overlaps with this round's own outgoing prompt"

    # Identity check: character-identical to the agent's own previous reply
    # means the DOM read landed on old content, not a new generation.
    history = load_session(sid, name)
    prev = next((m.content for m in reversed(history) if m.role == "assistant"), None)
    if prev is not None and prev.strip() == stripped:
        return False, "identity match — capture is identical to this agent's previous reply (stale read)"

    return True, None


def _extract_reply(env) -> tuple[str, bool, dict]:
    """Unpack a browser_relay lineage envelope.

    Returns (payload_str, validated_bool, raw_envelope_dict).
    Accepts a plain string too (legacy / API-agent path) — treated as valid.
    """
    if isinstance(env, dict):
        return env.get("payload", ""), env.get("validated", True), env
    return str(env), True, {}


def _render_agent_reply(name: str, content: str) -> None:
    """Render one agent's turn -- a real reply as a normal chat bubble, a
    transport failure as a visible system notice instead. Failures used to
    render in an identical st.chat_message bubble to a real reply, reading
    exactly like a panelist's turn instead of what it actually was.
    """
    if _is_relay_failure(content):
        st.error(f"⚠ {name.upper()} — transport failure this round: {content}")
    else:
        with st.chat_message(name, avatar=avatar(name)):
            st.markdown(f"**{name.upper()}**\n\n{content}")


def _build_cross_context(sid: str, my_name: str, participants: dict) -> str:
    """Return the last SUCCESSFUL reply from every other participant.

    Applies a tighter per-agent cap when total cross-context exceeds
    _CROSS_CONTEXT_THRESHOLD to prevent runaway prompt growth in long sessions.
    Bad captures (echoes, empty strings) are excluded regardless of cap.
    """
    # First pass: collect latest valid replies and measure total size
    raw: dict[str, str] = {}
    for name in participants:
        if name == my_name:
            continue
        history = load_session(sid, name)
        last = next(
            (m.content for m in reversed(history)
             if m.role == "assistant"
             and not _is_relay_failure(m.content)
             and not _is_bad_capture(m.content)),
            None,
        )
        if last:
            raw[name] = last

    if not raw:
        return ""

    total = sum(len(v) for v in raw.values())
    tight = total > _CROSS_CONTEXT_THRESHOLD
    cap   = _CROSS_CONTEXT_TIGHT_CAP if tight else _MAX_CROSS_CHARS

    parts = []
    for name, last in raw.items():
        snippet = last[:cap]
        if len(last) > cap:
            suffix = ("\n[…condensed — context condensed to stay within size limits]"
                      if tight else "\n[…truncated — see full reply in browser]")
            snippet += suffix
        parts.append(f"[{name.upper()}]: {snippet}")

    header = "[Context condensed — long session. Focus on the most recent points.]\n\n" if tight else ""
    return header + "The other panelists said:\n\n" + "\n\n".join(parts)


def _prepare_attachment(sid: str, name: str, round_id: int, full_msg: str) -> "dict | None":
    """Opt-in "File mode" hook: write `full_msg` to disk and return an
    attachment dict ({"path", "pointer"}) for browser_relay's _attach_file,
    or None if File mode is off or the message is small enough to just
    paste inline as today.

    browser_relay falls back to typing `full_msg` itself if the (unverified)
    file-input selectors fail to attach — File mode can only skip a big
    paste, never drop content.
    """
    if not st.session_state.get(f"ctrl_file_mode_{sid}", False):
        return None
    if len(full_msg) < _FILE_ATTACH_THRESHOLD_CHARS:
        return None
    try:
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = CONTEXT_DIR / f"context_{sid}_{name}_r{round_id}_{stamp}.txt"
        path.write_text(full_msg, encoding="utf-8")
    except Exception:
        return None
    pointer = (
        "This round's full context (other panelists' replies and/or the "
        "system prompt, plus your next prompt) is attached as a text file — "
        "please read it and respond accordingly."
    )
    return {"path": str(path), "pointer": pointer}


def _build_browser_msg(sid: str, name: str, cfg, active: dict, user_msg: str) -> str:
    """Build the message string to paste into a browser agent's chat box.

    Must be called BEFORE the current user_msg is appended to the agent's
    history file — is_first is determined by whether the agent has ever
    replied, not by whether there are user messages in the file.

    Round 1: send the enriched session system prompt (group identity + topic
             framing written at session creation) so the agent knows who it
             is and who else is in the conversation.
    Round 2+: omit system prompt (already in browser's conversation memory);
              send the other agents' latest replies so this agent can respond
              to them, followed by the user's new message.
    """
    history  = load_session(sid, name)   # read current state, before this round's append
    # A failed round (tab not found, send never verified, etc.) does not
    # count as "replied" -- otherwise a round-1 failure would permanently
    # skip sending this agent the system prompt on every later attempt.
    is_first = not any(
        m.role == "assistant" and not _is_relay_failure(m.content) for m in history
    )

    # Concise mode: opt-in toggle (Debug/control panel), off by default. Trims
    # payload size without touching the existing cross-context truncation caps
    # or transport/tab logic at all.
    concise = st.session_state.get(f"ctrl_concise_{sid}", False)
    concise_instr = "\n\n(Answer in 2–4 short paragraphs. Be direct.)" if concise else ""

    if is_first:
        # Use the enriched system prompt built at session creation (stored in the
        # session's system message) — NOT cfg.system_prompt, which is the raw yaml
        # field and lacks the group-conversation framing ("You are in a panel with…").
        sys_msg = next((m.content for m in history if m.role == "system"), cfg.system_prompt)
        # The browser tab may have prior conversation history from a different session.
        # The separator line signals a hard context break so the model doesn't answer
        # whatever was previously in the window.
        return (
            "=== New relay session starting ===\n\n"
            f"{sys_msg}\n\n---\n\n{user_msg}{concise_instr}"
        )

    cross = _build_cross_context(sid, name, active)
    msg = f"{cross}\n\n---\n\nUser: {user_msg}" if cross else user_msg
    return msg + concise_instr


def run_round(sid: str, participants: dict, user_msg: str, synthesize: bool = True,
              round_label: str | None = None) -> int:
    """Run one round. Returns the round_id stamped onto every display event
    this call writes, for auditing/debugging (e.g. matching a payload dump
    in the 🔍 expander to exactly which round produced it).

    round_id lives only in meta.json + display.jsonl (app.py's own files) --
    deliberately NOT stamped into relay.py's per-agent Message/.txt storage,
    which is a separate, more sophisticated canonical-transcript system with
    its own CLI and test suite that this polish pass isn't touching.
    """
    meta = load_meta(sid)
    round_id = meta.get("round_count", 0) + 1
    meta["round_count"] = round_id
    save_meta(sid, meta)

    label = round_label or f"Round {round_id}"
    st.caption(label)
    append_display(sid, {"type": "auto_round_marker", "content": label, "round_id": round_id})

    mgr = get_manager()

    def is_ready(name: str, cfg) -> bool:
        if getattr(cfg, "mode", "api") == "browser":
            return bool(mgr and mgr.has_agent(name))
        return agent_active(cfg)

    active = {n: c for n, c in participants.items() if is_ready(n, c)}
    if not active:
        st.warning("No agents are ready — link tabs in Agents or set API keys.")
        return round_id

    browser_agents = {n: c for n, c in active.items()
                      if getattr(c, "mode", "api") == "browser"}
    api_agents     = {n: c for n, c in active.items()
                      if getattr(c, "mode", "api") != "browser"}

    # Build browser payloads BEFORE appending the user message to state.
    # is_first inside _build_browser_msg checks whether the agent has replied
    # before — that check is only valid on the pre-update history.
    payloads: dict = {}
    attachments: dict = {}
    construction_ms: dict = {}
    for name, cfg in browser_agents.items():
        _t0 = time.time()
        payloads[name] = _build_browser_msg(sid, name, cfg, active, user_msg)
        attach = _prepare_attachment(sid, name, round_id, payloads[name])
        if attach:
            attachments[name] = attach
        construction_ms[name] = int((time.time() - _t0) * 1000)
        _log.info(f"[sid={sid}, round={round_id}, agent={name}] "
                 f"prompt_chars={len(payloads[name])}")

    # Diagnostic dump: capture the EXACT string about to be typed into each
    # tab, before send_batch touches the (possibly still-flaky) browser
    # transport. Written to disk now (ground truth, before anything else
    # runs); shown in the UI further below, after the user message, so the
    # transcript reads in the order things actually happened.
    if payloads:
        _dump_payloads_to_disk(payloads)

    # Now commit the user turn to all agent histories and the display log.
    append_display(sid, {"type": "user", "content": user_msg, "round_id": round_id})
    if payloads:
        append_display(sid, {"type": "payload_dump", "payloads": payloads, "round_id": round_id})
    for name in active:
        append_message(sid, name, Message("user", user_msg))

    replies:  dict = {}
    raw_envs: dict = {}  # {agent: raw envelope dict} — for display lineage metadata

    # Track last round prompt so Retry Failed Agents can re-use it
    st.session_state[f"last_round_prompt_{sid}"] = user_msg

    # ── Browser agents — submit all at once, wait in parallel ─────────────────
    if browser_agents and payloads and mgr:
        _set_agent_statuses({n: "Sending" for n in browser_agents})
        agent_names = list(payloads.keys())

        # send_batch() itself only returns once EVERY agent is done -- it
        # doesn't trickle results out as they land. Run it on a plain Python
        # thread (it just blocks on BrowserManager's command queue; no
        # Playwright call happens off the single worker thread) so this
        # thread can poll mgr.batch_progress() and update the spinner text
        # as agents finish, instead of showing a static agent list for
        # however long the single slowest agent takes.
        _outcome: dict = {}
        def _run_batch():
            try:
                _outcome["batch"] = mgr.send_batch(payloads, attachments=attachments)
            except Exception as e:
                _outcome["error"] = e
        _t = threading.Thread(target=_run_batch, daemon=True)
        st.session_state.round_in_progress = sid
        try:
            _t.start()

            status_box = st.empty()
            while _t.is_alive():
                prog    = mgr.batch_progress()
                waiting = [n for n in agent_names if not prog.get(n)]
                done    = [n for n in agent_names if prog.get(n)]
                if waiting:
                    text = f"⏳ Waiting for {', '.join(waiting)}…"
                    if done:
                        text += f"  ✅ done: {', '.join(done)}"
                else:
                    text = "⏳ Finishing up…"
                status_box.markdown(text)
                _t.join(timeout=0.4)
            status_box.empty()
        finally:
            st.session_state.round_in_progress = None

        if "error" in _outcome:
            e = _outcome["error"]
            _log.error(f"[sid={sid}, round={round_id}] Batch transport error — {e}", exc_info=True)
            st.error(f"⚠️ System Failure Notice: Batch transport error — {e}")
            batch = {}
        else:
            batch = _outcome.get("batch", {})

        transport_stats = {"confirmed": 0, "semantic_unconfirmed": 0,
                           "quarantined": 0, "transport_failure": 0}

        for name, env in batch.items():
            payload, validated, raw = _extract_reply(env)
            original_is_failure = _is_relay_failure(payload)  # before any app.py mutation
            # Stamp round_id into the envelope before any other mutation (fix 2:
            # was patched after-the-fact; now complete at write time)
            if isinstance(env, dict):
                env["round_id"] = round_id

            raw_payload    = payload  # preserve original captured text for quarantine record
            discard_reason: "str | None" = None
            quarantined    = False

            # Reject stale DOM reads before they enter the relay as real content --
            # EXCEPT capture_method == "semantic_unconfirmed": browser_relay.py's
            # grace-retry already gave the semantic count several extra chances to
            # catch up before concluding it's stale, and only assigns this method
            # when the count still never moved BUT the captured text itself is
            # real, substantial, and not a known failure sentinel. Discarding that
            # anyway was the actual bug behind the "8→8"/"0→0" false quarantines --
            # a genuinely good reply thrown away because one counter was slow.
            if (not validated and not _is_relay_failure(payload)
                    and raw.get("capture_method") != "semantic_unconfirmed"):
                pre = raw.get("pre_response_count", "?")
                nw  = raw.get("new_response_index", "?")
                discard_reason = (f"Stale DOM read — element count {pre}→{nw}, "
                                  f"new response not confirmed")
                payload     = f"[Error: {discard_reason}]"
                quarantined = True
            elif not validated and raw.get("capture_method") == "semantic_unconfirmed":
                # semantic_unconfirmed is meant to be a rare escape hatch, not a
                # second normal path -- a bare "non-empty, non-sentinel" check
                # can't tell a genuine new reply from an accidental echo of what
                # was just sent, or a stale read of the agent's OWN unchanged
                # previous turn. Both look "real" by that check alone.
                outgoing = payloads.get(name, "")
                passed, fail_reason = _semantic_unconfirmed_passes_strict_checks(
                    payload, outgoing, sid, name
                )
                if not passed:
                    pre = raw.get("pre_response_count", "?")
                    nw  = raw.get("new_response_index", "?")
                    discard_reason = f"Unconfirmed capture failed strict check — {fail_reason} (count {pre}→{nw})"
                    payload     = f"[Error: {discard_reason}]"
                    quarantined = True
                    _log.warning(f"[sid={sid}, round={round_id}, agent={name}] "
                                f"semantic_unconfirmed REJECTED — {fail_reason}")
                else:
                    _log.info(f"[sid={sid}, round={round_id}, agent={name}] "
                             f"Lower-confidence capture kept (semantic_unconfirmed, passed strict checks)")

            # Quality validation — second line of defense (e.g. prompt echo)
            if not _is_relay_failure(payload):
                quality_ok, quality_reason = _validate_payload_quality(payload)
                if not quality_ok:
                    discard_reason = f"Payload quality — {quality_reason}"
                    raw_payload    = payload  # capture the text that failed quality
                    payload        = f"[Error: {discard_reason}]"
                    quarantined    = True

            # Stamp quarantine fields into the envelope so they land in display.jsonl
            if isinstance(raw, dict):
                raw["quarantined"]    = quarantined
                raw["discard_reason"] = discard_reason
                raw["raw_payload"]    = raw_payload if quarantined else None

            if quarantined:
                _log.warning(f"[sid={sid}, round={round_id}, agent={name}] Quarantined — {discard_reason}")
            elif _is_relay_failure(payload):
                _log.warning(f"[sid={sid}, round={round_id}, agent={name}] Relay failure — {payload[:120]}")

            # Tally for the per-round transport-stats summary -- the actual
            # success metric is confirmed rate, not "no failures": a round
            # that passes only via semantic_unconfirmed is limping, not fixed.
            if original_is_failure:
                transport_stats["transport_failure"] += 1
            elif quarantined:
                transport_stats["quarantined"] += 1
            elif raw.get("capture_method") == "semantic_unconfirmed":
                transport_stats["semantic_unconfirmed"] += 1
            else:
                transport_stats["confirmed"] += 1

            # Speed/quality diagnostics for this agent this round -- rides through
            # display.jsonl automatically via the existing env_meta mechanism.
            if original_is_failure:
                reply_state = "transport_failure"
            elif quarantined:
                reply_state = "quarantined"
            elif raw.get("capture_method") == "semantic_unconfirmed":
                reply_state = "semantic_unconfirmed"
            else:
                reply_state = "confirmed"

            if quarantined:
                failure_stage  = raw.get("failure_stage") or "validation"
                failure_reason = discard_reason
            elif original_is_failure:
                failure_stage  = raw.get("failure_stage") or "transport"
                failure_reason = payload
            else:
                failure_stage  = None
                failure_reason = None

            timing = raw.get("timing") or {}
            if isinstance(raw, dict):
                attach = attachments.get(name)
                raw["diagnostics"] = {
                    "round_id":           round_id,
                    "agent":              name,
                    "prompt_chars":       len(payloads.get(name, "") or ""),
                    "files_included":     [attach["path"]] if attach else [],
                    "context_mode":       raw.get("context_mode") or "inline",
                    "tab_classification": raw.get("site_key") or None,
                    "construction_ms":    construction_ms.get(name),
                    "insertion_ms":       timing.get("insertion_ms"),
                    "send_verify_ms":     timing.get("send_verify_ms"),
                    "wait_response_ms":   timing.get("wait_response_ms"),
                    "capture_ms":         None,
                    "capture_method":     raw.get("capture_method"),
                    "reply_state":        reply_state,
                    "failure_stage":      failure_stage,
                    "failure_reason":     failure_reason,
                }

            raw_envs[name] = raw
            replies[name]  = payload
            status = "Failed" if _is_relay_failure(payload) else "Done"
            _set_agent_statuses({name: status})

        if batch:
            append_display(sid, {"type": "transport_stats", "round_id": round_id,
                                 "stats": transport_stats})
            _log.info(f"[sid={sid}, round={round_id}] Transport stats — {transport_stats}")

        for name in browser_agents:
            if name not in replies:
                continue
            _render_agent_reply(name, replies[name])

    # ── API agents — sequential with streaming ─────────────────────────────────
    for name, cfg in api_agents.items():
        with st.chat_message(name, avatar=avatar(name)):
            st.markdown(f"**{name.upper()}**")
            try:
                reply = st.write_stream(stream_agent(cfg, load_session(sid, name)))
                replies[name] = reply
            except Exception as e:
                _log.error(f"[sid={sid}, round={round_id}, agent={name}] API agent failed — {e}", exc_info=True)
                st.error(f"**{name.upper()} failed:** {e}")

    # ── Persist replies ────────────────────────────────────────────────────────
    for name, reply in replies.items():
        # Store lineage envelope metadata (without the payload itself to avoid duplication)
        env_meta = {k: v for k, v in raw_envs.get(name, {}).items() if k != "payload"}
        append_display(sid, {"type": "reply", "agent": name, "content": reply,
                             "round_id": round_id, "envelope": env_meta})
        append_message(sid, name, Message("assistant", reply))

    # Cross-inject all other agents' replies as ONE bundled user message.
    # Multiple back-to-back append_message("user") calls create consecutive
    # user-role entries which Anthropic's API rejects (400) if any API agents
    # are ever added to a session.
    #
    # Only SUCCESSFUL replies get cross-injected/synthesized -- a failed tab
    # ("[Error: ...]", "[Timed out]", etc.) is shown to the user above so the
    # failure is visible, but every other agent earnestly responding to a
    # sentinel string as if it were a real position is exactly how one tab
    # hiccup corrupts the whole panel's context for the rest of the session.
    successful_replies = {n: r for n, r in replies.items() if not _is_relay_failure(r)}

    for name in active:
        cross_parts = [
            f"[{other.upper()}]: {reply}"
            for other, reply in successful_replies.items()
            if other != name
        ]
        if cross_parts:
            append_message(sid, name, Message("user", "\n\n".join(cross_parts)))

    # ── Synthesizer ───────────────────────────────────────────────────────────
    if synthesize and len(successful_replies) > 1:
        run_synthesis(sid, user_msg, successful_replies, round_id=round_id, mgr=mgr)

    # ── GitHub (background) ───────────────────────────────────────────────────
    if replies:
        repo = get_github_repo()
        if repo:
            meta = load_meta(sid)
            commit_async(_gh_append_round, repo, meta.get("topic", "conversation"),
                         list(active.keys()), user_msg, replies)

    return round_id


_AUTO_CONTINUE_MSG = (
    "Continue the discussion. Read the other panelists' latest replies "
    "above and respond directly. Do not restart the topic."
)

_FINAL_SYNTHESIS_PROMPT = (
    "This is the final synthesis round. Based on everything discussed so far:\n\n"
    "1. State your single strongest conclusion — what you are most confident about.\n"
    "2. Flag the most critical remaining risk or open question.\n"
    "3. Based on your role in this panel, name one concrete next action "
    "you are taking ownership of.\n\n"
    "Be direct and specific. This round produces the handoff brief."
)

_TASK_ASSIGNMENT_PROMPT = (
    "Task assignment round. Discussion happens first, assignment second --"
    " this round is the second part, not more discussion.\n\n"
    "1. State the single most valuable next sub-task that should happen now, "
    "based on everything discussed so far.\n"
    "2. Propose which panelist (by name) should own it, based on the role "
    "and strengths they've actually shown in this discussion -- it does not "
    "have to be you.\n"
    "3. If no one is clearly better suited, state one concrete thing you "
    "personally will do next.\n\n"
    "Be concrete and specific -- name a real deliverable, not a vague direction."
)


def _set_agent_statuses(updates: dict) -> None:
    """Write agent status tiles to session state. Called synchronously in run_round."""
    if "agent_statuses" not in st.session_state:
        st.session_state.agent_statuses = {}
    st.session_state.agent_statuses.update(updates)

# ── Auto-loop state machine ─────────────────────────────────────────────────
#
# Replaces the previous design (a single Python while-loop, inside one
# continuous run_auto_rounds() call, checking a flag between iterations).
# That design's safety depended on an unproven assumption about Streamlit's
# script-interruption behavior: a Stop click during a long-running script
# might tear down execution wherever it happened to be (mid round), not
# cleanly between rounds. This design has no such assumption to make --
# every round is its own complete Streamlit script execution with a real
# st.rerun() boundary between it and the next one. Stop is a normal button
# click processed BETWEEN executions, the same as any other widget
# interaction; there's nothing left running for it to interrupt.

def _auto_loop_key(sid: str) -> str:
    return f"auto_loop_{sid}"

def _get_auto_loop(sid: str) -> dict | None:
    return st.session_state.get(_auto_loop_key(sid))

def _start_auto_loop(sid: str, n_rounds: int | None, synthesize: bool) -> None:
    """Call AFTER round 1 has already run via a normal run_round() call.
    Arms the state machine so _advance_auto_loop continues it on
    subsequent reruns -- round 1 itself is not part of this state machine,
    it's just a normal manual round that happens to be followed by one.
    """
    st.session_state[_auto_loop_key(sid)] = {
        "active": True, "rounds_done": 1, "n_rounds": n_rounds, "synthesize": synthesize,
    }

def _stop_auto_loop(sid: str) -> None:
    state = _get_auto_loop(sid)
    if state:
        state["active"] = False

def _resume_auto_loop(sid: str) -> None:
    state = _get_auto_loop(sid)
    if state:
        state["active"] = True

def _auto_loop_is_stopped(sid: str) -> bool:
    state = _get_auto_loop(sid)
    return bool(state) and not state.get("active", True)

def _advance_auto_loop(sid: str, participants: dict) -> None:
    """Run exactly one more round of an active auto-loop, then rerun.

    Call once per script execution, after the control panel (so Stop/Resume
    clicks from THIS render are already applied) and before the chat input.
    A no-op if there's no active loop -- the rest of the script proceeds
    normally to render the chat input in that case.
    """
    state = _get_auto_loop(sid)
    if not state or not state.get("active"):
        return
    n_rounds    = state.get("n_rounds")
    rounds_done = state.get("rounds_done", 1)
    if n_rounds is not None and rounds_done >= n_rounds:
        st.session_state.pop(_auto_loop_key(sid), None)
        return

    next_round = rounds_done + 1
    label  = f"Round {next_round}" if n_rounds is None else f"Round {next_round}/{n_rounds}"
    marker = f"🔁 {label} — auto-continuing"
    run_round(sid, participants, _AUTO_CONTINUE_MSG,
             synthesize=state.get("synthesize", True), round_label=marker)

    state["rounds_done"] = next_round
    if n_rounds is not None and next_round >= n_rounds:
        st.session_state.pop(_auto_loop_key(sid), None)
    st.rerun()


def retry_failed_agents(sid: str, participants: dict) -> int:
    """Re-send last round's prompt only to agents that failed. Returns count of retried agents.

    Retry results attach to the same round_id so they appear in the same
    round group in render_display. Successful retries are cross-injected;
    continued failures stay as system notices. Does not advance round_count.
    """
    meta     = load_meta(sid)
    round_id = meta.get("round_count", 0)
    if round_id == 0:
        return 0

    p = display_path(sid)
    if not p.exists():
        return 0

    events = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    last_replies = {
        e["agent"]: e["content"]
        for e in events
        if e.get("type") == "reply" and e.get("round_id") == round_id
    }
    failed_names = {n for n, c in last_replies.items() if _is_relay_failure(c)}
    if not failed_names:
        return 0

    failed_participants = {n: c for n, c in participants.items() if n in failed_names}
    last_prompt = st.session_state.get(f"last_round_prompt_{sid}", _AUTO_CONTINUE_MSG)
    mgr = get_manager()
    if not mgr:
        return 0

    browser_failed = {n: c for n, c in failed_participants.items()
                      if getattr(c, "mode", "api") == "browser" and mgr.has_agent(n)}
    if not browser_failed:
        return 0

    payloads = {n: _build_browser_msg(sid, n, c, participants, last_prompt)
                for n, c in browser_failed.items()}
    attachments = {}
    for n in browser_failed:
        attach = _prepare_attachment(sid, n, round_id, payloads[n])
        if attach:
            attachments[n] = attach

    _set_agent_statuses({n: "Sending" for n in browser_failed})
    st.session_state.round_in_progress = sid
    try:
        with st.spinner(f"Retrying {', '.join(browser_failed)} …"):
            try:
                batch = mgr.send_batch(payloads, attachments=attachments)
            except Exception as e:
                _log.error(f"[sid={sid}, round={round_id}] Retry transport error — {e}", exc_info=True)
                st.error(f"⚠️ Retry transport error: {e}")
                return 0
    finally:
        st.session_state.round_in_progress = None

    new_replies: dict = {}
    for name, env in batch.items():
        payload, validated, raw = _extract_reply(env)
        if isinstance(env, dict):
            env["round_id"] = round_id
        if not validated and not _is_relay_failure(payload):
            pre = raw.get("pre_response_count", "?")
            nw  = raw.get("new_response_index", "?")
            payload = f"[Error: Retry stale DOM read — element count {pre}→{nw}]"
        if not _is_relay_failure(payload):
            quality_ok, quality_reason = _validate_payload_quality(payload)
            if not quality_ok:
                payload = f"[Error: Retry quality — {quality_reason}]"

        env_meta = {k: v for k, v in raw.items() if k != "payload"} if isinstance(raw, dict) else {}
        append_display(sid, {"type": "reply", "agent": name, "content": payload,
                             "round_id": round_id, "envelope": env_meta, "retry": True})
        _render_agent_reply(name, payload)

        if not _is_relay_failure(payload):
            append_message(sid, name, Message("assistant", payload))
            new_replies[name] = payload
            _set_agent_statuses({name: "Done"})
        else:
            _set_agent_statuses({name: "Failed"})

    # Cross-inject only the new successful replies into the other agents' contexts
    for name, reply in new_replies.items():
        for other in participants:
            if other != name:
                append_message(sid, other, Message("user", f"[{name.upper()}]: {reply}"))

    return len(browser_failed)


# ── Session state ─────────────────────────────────────────────────────────────

if "active_session"   not in st.session_state: st.session_state.active_session   = None
if "edit_agent"       not in st.session_state: st.session_state.edit_agent       = None
if "agent_statuses"   not in st.session_state: st.session_state.agent_statuses   = {}
if "round_in_progress" not in st.session_state: st.session_state.round_in_progress = None
if "cached_active_agents" not in st.session_state: st.session_state.cached_active_agents = {}

# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

# active_agents() does a blocking queue round-trip (list_agents()) against the
# SAME worker queue a long-running send_batch() occupies for the whole round.
# If a round is in flight (round_in_progress set), that call would queue
# behind it and time out after 5s on every single rerun -- including reruns
# from "instant" actions like Stop After Round -- making the UI falsely
# report no agents connected and feel hung. Skip it and reuse the last good
# snapshot while a round is in progress; refresh normally otherwise.
if st.session_state.round_in_progress:
    active = st.session_state.cached_active_agents
else:
    active = active_agents()
    st.session_state.cached_active_agents = active

with st.sidebar:
    st.title("⚡ Relay")
    mode = st.radio("mode", ["Conversations", "Agents", "Settings"],
                    horizontal=True, label_visibility="collapsed")
    st.divider()

    if mode == "Conversations":
        sessions = all_sessions()
        if sessions:
            st.subheader("Sessions")
            for s in sessions:
                c1, c2 = st.columns([5, 1])
                with c1:
                    is_sel = st.session_state.active_session == s.name
                    label  = ("▶ " if is_sel else "") + session_label(s.name)
                    busy_other = (st.session_state.round_in_progress
                                  and st.session_state.round_in_progress != s.name)
                    if st.button(label, key=f"s_{s.name}", use_container_width=True,
                                 disabled=bool(busy_other)):
                        st.session_state.active_session = s.name; st.rerun()
                with c2:
                    if st.button("🗑", key=f"d_{s.name}",
                                 disabled=bool(st.session_state.round_in_progress)):
                        if st.session_state.active_session == s.name:
                            st.session_state.active_session = None
                        delete_session(s.name); st.rerun()
        else:
            st.caption("No conversations yet.")

        st.divider()
        st.subheader("New Conversation")
        if active:
            st.caption("  ".join(f"{avatar(n)} {n.upper()}" for n in active))
        else:
            st.caption("⚠ No agents active — launch browsers in **Agents** first.")
        if st.session_state.round_in_progress:
            st.caption(f"⏳ A round is in progress in **{st.session_state.round_in_progress}** "
                       "— wait for it to finish or stop it first.")
        topic        = st.text_input("Topic", placeholder="e.g. Is AGI near?")
        new_chat_btn = st.button("＋ Start", type="primary", use_container_width=True,
                                  disabled=bool(st.session_state.round_in_progress))

    elif mode == "Agents":
        st.subheader("Agents")
        for name, cfg in agents_cfg.items():
            is_on = agent_active(cfg)
            if st.button(f"{avatar(name)} {name.upper()}  {'✓' if is_on else '○'}",
                         key=f"sel_{name}", use_container_width=True):
                st.session_state.edit_agent = name
        st.divider()
        if st.button("＋ New Agent", type="primary", use_container_width=True):
            st.session_state.edit_agent = "__new__"

    else:  # Settings
        st.caption(f"Anthropic  {'✓' if os.environ.get('ANTHROPIC_API_KEY') else '○'}")
        st.caption(f"OpenAI     {'✓' if os.environ.get('OPENAI_API_KEY') else '○'}")
        repo = get_github_repo()
        st.caption(f"GitHub     {'✓' if repo else '○'}")

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

# ── CONVERSATIONS ─────────────────────────────────────────────────────────────

if mode == "Conversations":
    if new_chat_btn:
        if not topic.strip():
            st.warning("Enter a topic.")
        elif not agents_cfg:
            st.warning("No agents configured — add some in **Agents**.")
        else:
            # Include all configured agents in the session; they join when launched
            all_names = list(agents_cfg.keys())
            ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            sid = f"conv-{ts}"
            meta = {
                "session_id": sid, "type": "group_chat",
                "topic":      topic.strip(), "agents": all_names,
                "created":    datetime.datetime.now().isoformat(),
                "round_count": 0,
            }
            save_meta(sid, meta)
            for name, cfg in agents_cfg.items():
                others = [n for n in all_names if n != name]
                sys_prompt = (
                    f"You are {name.upper()}. You are in a multi-agent relay conversation "
                    f"with {', '.join(o.upper() for o in others)}.\n"
                    f"Topic: {topic.strip()}\n\n"
                    f"HOW THE RELAY WORKS:\n"
                    f"- Each round you receive the other agents' latest replies tagged as [NAME]: their message\n"
                    f"- You must read those tagged messages and respond to them directly — they are real\n"
                    f"  replies from the other agents, not placeholders\n"
                    f"- Your reply is then forwarded to all other agents before the next round\n"
                    f"- Write ONE response per round. Do not simulate other agents or produce multiple turns.\n\n"
                ) + cfg.system_prompt
                append_message(sid, name, Message("system", sys_prompt))
            st.session_state.active_session = sid
            st.rerun()

    elif st.session_state.active_session:
        sid  = st.session_state.active_session
        meta = load_meta(sid)
        if not meta:
            st.session_state.active_session = None; st.rerun()

        session_agents = meta.get("agents", [])
        participants   = {n: agents_cfg[n] for n in session_agents
                          if n in agents_cfg and agent_active(agents_cfg[n])}
        missing        = [n for n in session_agents if n not in participants]

        st.header(meta.get("topic", "Conversation"))
        st.caption("  ·  ".join(f"{avatar(n)} {n.upper()}" for n in session_agents))

        if missing:
            st.warning(f"Not yet launched: {', '.join(n.upper() for n in missing)} — open them in **Agents** to join.")

        # GitHub link
        repo = get_github_repo()
        if repo:
            topic_val = meta.get("topic", "conversation")
            st.caption(
                f"📎 [GitHub thread]({_gh_html_url(repo, topic_val)})  ·  "
                f"[Raw for agents]({_gh_raw_url(repo, topic_val)})"
            )

        render_display(sid)

        if not participants:
            st.info("No browsers open yet — launch agents in the **Agents** tab, then come back and send a message.")
        else:
            # ── Compact Operator Control Panel ────────────────────────────────
            # Initialize session-backed control state so values survive reruns.
            _k_agents  = f"ctrl_agents_{sid}"
            _k_rounds  = f"ctrl_rounds_{sid}"
            _k_synth   = f"ctrl_synth_{sid}"
            _k_concise = f"ctrl_concise_{sid}"
            _k_filemode = f"ctrl_file_mode_{sid}"
            # Seed defaults only on first render for this session
            if _k_agents not in st.session_state:
                st.session_state[_k_agents] = list(participants.keys())
            else:
                # Drop any stored names that are no longer active participants
                valid = [n for n in st.session_state[_k_agents] if n in participants]
                st.session_state[_k_agents] = valid if valid else list(participants.keys())

            with st.container(border=True):
                cp1, cp2, cp3 = st.columns([4, 2, 1])
                with cp1:
                    selected_names = st.multiselect(
                        "Agents this round",
                        options=list(participants.keys()),
                        default=st.session_state[_k_agents],
                        format_func=lambda n: f"{avatar(n)} {n.upper()}",
                        label_visibility="collapsed",
                        key=_k_agents,
                    )
                    round_participants = {n: participants[n] for n in selected_names}
                with cp2:
                    unlimited = st.checkbox("♾️ Unlimited rounds", key=f"ctrl_unlimited_{sid}")
                    auto_rounds = st.number_input(
                        "Max rounds", min_value=1, max_value=10, value=3,
                        label_visibility="collapsed",
                        key=_k_rounds,
                        disabled=unlimited,
                        help="Ignored while Unlimited is checked — use ⏹ Stop After Round to end an unlimited run.",
                    )
                with cp3:
                    st.metric("Round", meta.get("round_count", 0))

                ctrl1, ctrl2, ctrl3, ctrl4, ctrl5, ctrl6, ctrl7, ctrl8 = st.columns(8)
                with ctrl1:
                    synthesize = st.toggle("🔮 Synthesize", value=True, key=_k_synth)
                with ctrl2:
                    stop_btn = st.button("⏹ Stop After Round", use_container_width=True)
                    if stop_btn:
                        _stop_auto_loop(sid)
                        st.rerun()
                with ctrl3:
                    final_synth_btn = st.button("🎯 Final Synthesis", use_container_width=True,
                                                help="Run one final round prompting each agent to state their "
                                                     "strongest conclusion, flag remaining risks, and delegate tasks.")
                with ctrl4:
                    retry_btn = st.button("🔄 Retry Failed", use_container_width=True,
                                          help="Re-send last round's prompt to failed agents only.")
                with ctrl5:
                    assign_tasks_btn = st.button("📋 Assign Tasks", use_container_width=True,
                                                 help="Ask each agent to propose the next concrete sub-task "
                                                      "and who (by name) should own it.")
                with ctrl6:
                    preflight_btn = st.button("🛫 Preflight", use_container_width=True,
                                              help="Check tab identity, URL, input visibility, and semantic "
                                                   "count for every selected agent — no prompt sent.")
                with ctrl7:
                    concise = st.toggle("✂️ Concise", value=False, key=_k_concise,
                                        help="Instruct agents to answer in 2–4 short paragraphs and use "
                                             "only the latest reply from each other agent (not the full "
                                             "transcript) as cross-context — shrinks prompt size, doesn't "
                                             "change tab/transport behavior.")
                with ctrl8:
                    file_mode = st.toggle("📎 File mode", value=False, key=_k_filemode,
                                          help="Experimental, off by default. When a round's outgoing "
                                               "message is large, write it to a local file and try to "
                                               "attach it instead of pasting the full text — meant to "
                                               "avoid large-paste batch errors on some sites. File-input "
                                               "selectors are unverified guesses: if attaching fails, the "
                                               "full text is typed inline instead, so content is never "
                                               "silently dropped.")

                _loop = _get_auto_loop(sid)
                if _loop is None:
                    stop_status = "⚪ No auto-loop"
                elif _loop.get("active"):
                    rd, nr = _loop.get("rounds_done", 1), _loop.get("n_rounds")
                    stop_status = f"🟢 Running (round {rd}" + (f"/{nr})" if nr else ", unlimited)")
                else:
                    stop_status = "🔴 Stopped"
                st.caption(stop_status)

            # ── Preflight check ───────────────────────────────────────────────
            if preflight_btn and mgr and round_participants:
                browser_participants = {n: c for n, c in round_participants.items()
                                        if getattr(c, "mode", "api") == "browser"}
                results = {n: mgr.fast_health_check(n) for n in browser_participants}
                st.session_state[f"preflight_{sid}"] = results

            preflight_results = st.session_state.get(f"preflight_{sid}")
            if preflight_results:
                with st.expander("🛫 Preflight results", expanded=True):
                    for name, r in preflight_results.items():
                        ok = r.get("ok")
                        icon = "🟢" if ok else "🔴"
                        marker = r.get("marker") or "(unmarked)"
                        sc = r.get("semantic_count")
                        sc_str = str(sc) if sc is not None else "?"
                        st.caption(
                            f"{icon} **{name.upper()}** — url: `{r.get('url', '?')}` · "
                            f"marker: `{marker}` · semantic_count: {sc_str}"
                            + (f" · ⚠ {r.get('reason')}" if not ok else "")
                        )

            # ── Agent Status Grid ─────────────────────────────────────────────
            statuses = st.session_state.get("agent_statuses", {})
            if statuses:
                status_cols = st.columns(len(statuses))
                _STATUS_ICONS = {
                    "Ready": "⬜", "Checking": "🟠", "Healthy": "🟢",
                    "Sending": "🟡", "Waiting": "🔵", "Failed": "🔴", "Done": "🟢",
                }
                for col, (ag, st_val) in zip(status_cols, statuses.items()):
                    icon = _STATUS_ICONS.get(st_val, "⬜")
                    col.caption(f"{icon} {ag.upper()}\n{st_val}")

            # ── Duplicate tab warning ─────────────────────────────────────────
            # If two agents share the same Chrome tab, each prompt intended for
            # one agent is also pasted into the other's composer — causing the
            # "architect prompt appeared in optimist's tab" symptom.
            _mgr_dup = get_manager()
            if _mgr_dup:
                try:
                    _dup_groups = _mgr_dup.find_duplicate_tabs()
                    for _grp in _dup_groups:
                        st.warning(
                            f"⚠️ **Duplicate tab assignment**: {' and '.join(a.upper() for a in _grp)} "
                            f"are assigned to the **same** Chrome tab. "
                            "Prompts for one agent will land in the other's input. "
                            "Go to **Agents** → unassign one, then assign it to a different tab.",
                            icon="⚠️",
                        )
                except Exception:
                    pass

            # ── Export ────────────────────────────────────────────────────────
            if display_path(sid).exists():
                ex1, ex2 = st.columns(2)
                with ex1:
                    st.download_button(
                        "📄 Export Clean Transcript",
                        data=_generate_clean_transcript(sid),
                        file_name=f"relay-{sid}-clean.md",
                        mime="text/markdown",
                        use_container_width=True,
                    )
                with ex2:
                    st.download_button(
                        "📋 Export Debug Transcript",
                        data=_generate_debug_transcript(sid),
                        file_name=f"relay-{sid}-debug.md",
                        mime="text/markdown",
                        use_container_width=True,
                    )

            # ── File attachment ───────────────────────────────────────────────
            uploaded_files = st.file_uploader(
                "📂 Drag & drop or click to attach — text, code, PDF, DOCX, XLSX, images and more",
                type=_UPLOAD_TYPES,
                accept_multiple_files=True,
                key=f"upload_{sid}",
            )
            upload_errors = []
            if uploaded_files:
                combined_parts = []
                for uf in uploaded_files:
                    try:
                        text = _read_uploaded_file(uf)
                        combined_parts.append((uf.name, text))
                    except Exception as e:
                        _log.error(f"[sid={sid}] File upload failed — {uf.name}: {e}", exc_info=True)
                        upload_errors.append(f"{uf.name}: {e}")
                if combined_parts:
                    # Merge all files into one staged attachment
                    merged_name  = ", ".join(n for n, _ in combined_parts)
                    merged_text  = "\n\n".join(
                        f"[File: {n}]\n```\n{t}\n```" for n, t in combined_parts
                    )
                    st.session_state[f"file_content_{sid}"] = (merged_name, merged_text)

            # Staged-file confirmation + X button — always rendered BEFORE
            # any error message so the button is never blocked by an error banner
            if f"file_content_{sid}" in st.session_state:
                fc_name, fc_text = st.session_state[f"file_content_{sid}"]
                col1, col2 = st.columns([6, 1])
                with col1:
                    st.caption(f"📎 **{fc_name}** ({len(fc_text):,} chars) — will be included in next message")
                with col2:
                    if st.button("✕", key="clear_file"):
                        del st.session_state[f"file_content_{sid}"]
                        st.rerun()

            for upload_error in upload_errors:
                st.warning(f"Could not read file: {upload_error}")

            # ── Retry Failed Agents ───────────────────────────────────────────
            if retry_btn:
                n_retried = retry_failed_agents(sid, participants)
                if n_retried == 0:
                    st.info("No failed agents to retry in the last round.")
                st.rerun()

            # ── Final Synthesis trigger ───────────────────────────────────────
            if final_synth_btn and round_participants:
                with st.spinner("Running final synthesis round…"):
                    run_round(sid, round_participants, _FINAL_SYNTHESIS_PROMPT,
                              synthesize=True, round_label="🎯 Final Synthesis")
                st.rerun()

            # ── Task Assignment trigger ───────────────────────────────────────
            # Discuss first, assign second: a deliberate, separate round rather
            # than folding "who does what" into every auto-continue prompt, so
            # the panel actually discusses before jumping to assignment.
            if assign_tasks_btn and round_participants:
                with st.spinner("Running task-assignment round…"):
                    run_round(sid, round_participants, _TASK_ASSIGNMENT_PROMPT,
                              synthesize=True, round_label="📋 Task Assignment")
                st.rerun()

            # ── Stop / resume button (inline near message area) ──────────────
            _stopped = _auto_loop_is_stopped(sid)
            _sb_col1, _sb_col2 = st.columns([10, 1])
            with _sb_col2:
                if _stopped:
                    if st.button("▶", key="resume_inline",
                                 help="Resume the auto-loop from where it stopped",
                                 use_container_width=True):
                        _resume_auto_loop(sid)
                        st.rerun()
                elif _get_auto_loop(sid):
                    if st.button("⏹", key="stop_inline",
                                 help="Stop the auto-loop after the round in flight finishes",
                                 use_container_width=True):
                        _stop_auto_loop(sid)
                        st.rerun()
            with _sb_col1:
                if _stopped:
                    st.caption("⏹ Loop stopped — click ▶ to resume, or type a message to start a new round")

            # ── Advance the auto-loop, if one is active ───────────────────────
            # Must run after the control panel (so a Stop/Resume click from
            # THIS render is already applied) and before the chat input.
            # Each call here runs exactly one round then reruns -- a no-op
            # when no loop is active, falling through to render chat input
            # normally.
            _advance_auto_loop(sid, round_participants)

            # ── Chat input ────────────────────────────────────────────────────
            if user_input := st.chat_input("Message all agents…"):
                file_attachment = st.session_state.pop(f"file_content_{sid}", None)
                if file_attachment:
                    fname, ftext = file_attachment
                    # ftext is already formatted as [File: name]\n```...``` blocks.
                    # Don't re-wrap: that produces double-nested code fences.
                    full_msg = f"{ftext}\n\n{user_input}" if user_input.strip() else ftext
                else:
                    full_msg = user_input
                with st.chat_message("user", avatar="🧑"):
                    if file_attachment:
                        st.caption(f"📎 {fname}")
                    st.markdown(user_input)
                # A new message replaces any previous loop state outright --
                # round 1 always runs as a normal manual round; _start_auto_loop
                # arms the state machine to continue it on subsequent reruns
                # only if more rounds were actually requested.
                st.session_state.pop(_auto_loop_key(sid), None)
                run_round(sid, round_participants, full_msg, synthesize=synthesize)
                if unlimited:
                    _start_auto_loop(sid, None, synthesize)
                elif auto_rounds > 1:
                    _start_auto_loop(sid, int(auto_rounds), synthesize)
                st.rerun()

    else:
        st.markdown("## ⚡ Relay")
        st.markdown("All active agents respond to every message and read each other's replies.")
        st.markdown("Start a conversation in the sidebar.")

# ── AGENTS ────────────────────────────────────────────────────────────────────

elif mode == "Agents":
    ANTHROPIC_MODELS = [
        "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001",
        "claude-3-5-sonnet-20241022",
    ]
    OPENAI_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]

    ea = st.session_state.edit_agent

    if ea is None:
        mgr          = get_manager()
        assigned_set = set(mgr.list_agents()) if mgr else set()
        st.header("Agents")

        # ── Chrome connection bar ─────────────────────────────────────────────
        if not PLAYWRIGHT_AVAILABLE:
            st.error(f"Playwright not available: {PLAYWRIGHT_ERROR}")
        else:
            with st.container(border=True):
                cc1, cc2, cc3 = st.columns([5, 2, 2])
                with cc1:
                    if mgr:
                        try:
                            tab_count = len(mgr.scan_tabs())
                            st.markdown(f"**🟢 Chrome connected** — {tab_count} tabs visible")
                        except Exception:
                            st.markdown("**🔴 Chrome disconnected**")
                    else:
                        st.markdown("**⚪ Chrome not connected**")
                        st.caption("Launch Chrome with Step 2 command in Settings → Chrome Setup. Sign in first with Step 1 (no debug port) so Google OAuth works.")
                with cc2:
                    if mgr:
                        if st.button("Re-scan tabs", use_container_width=True):
                            auto_assign_tabs(mgr); st.rerun()
                    else:
                        if st.button("Connect to Chrome", type="primary", use_container_width=True):
                            try:
                                connect_browser(); st.rerun()
                            except Exception as e:
                                st.session_state["connect_err"] = str(e); st.rerun()
                with cc3:
                    if mgr:
                        if st.button("Disconnect", use_container_width=True):
                            disconnect_browser(); st.rerun()

                err = st.session_state.pop("connect_err", None)
                if err:
                    if "connection refused" in err.lower() or "9222" in err or "connect" in err.lower():
                        st.error(
                            "Chrome not reachable on port 9222.\n\n"
                            "Run this in PowerShell to launch a Relay-ready Chrome:\n\n"
                            "```powershell\n"
                            'Start-Process "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                            '-ArgumentList "--remote-debugging-port=9222","--remote-allow-origins=*","--user-data-dir=C:\\relay-chrome-profile"\n'
                            "```\n\n"
                            "Then verify by visiting `localhost:9222` in that Chrome — should show a JSON page."
                        )
                    else:
                        st.error(err)

            if mgr:
                with st.expander("🔍 Scanned tabs (debug)"):
                    try:
                        scanned = mgr.scan_tabs()
                    except Exception as e:
                        scanned = []
                        st.caption(f"scan_tabs() failed: {e}")
                    rows = []
                    for i, tab in enumerate(scanned):
                        marker = tab.get("marker", "")
                        cls    = tab.get("classification", classify_tab_url(tab["url"]))
                        if marker:
                            reason = f"assigned to {marker}"
                        elif cls == "unsupported":
                            reason = "no known site matches this URL"
                        elif cls == "claude_other":
                            reason = "claude.ai but not chat composer or /code"
                        else:
                            reason = "available"
                        rows.append({
                            "index": i, "title": tab.get("title", "")[:40],
                            "url": tab["url"][:70], "classification": cls,
                            "assigned_to": marker or "—", "reason": reason,
                        })
                    if rows:
                        st.table(rows)
                    else:
                        st.caption("No tabs visible.")

        st.divider()
        if not agents_cfg:
            st.info("No agents yet — click **＋ New Agent** in the sidebar.")

        # ── Per-agent rows ────────────────────────────────────────────────────
        for name, cfg in agents_cfg.items():
            is_browser = getattr(cfg, "mode", "api") == "browser"
            has_tab    = is_browser and name in assigned_set
            is_on      = has_tab if is_browser else _api_agent_active(cfg)

            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([5, 2, 1, 1])
                with c1:
                    if is_browser:
                        site   = site_for_agent(name, getattr(cfg, "site_key", ""))
                        label  = site["key"] if site else "browser"
                        status = "✓" if has_tab else "○"
                        st.markdown(f"**{avatar(name)} {name.upper()}** &nbsp; {status} {label}")
                    else:
                        key_ok = bool(os.environ.get(
                            "ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY", ""
                        ))
                        st.markdown(f"**{avatar(name)} {name.upper()}** &nbsp; {'✓' if key_ok else '○'} {cfg.provider}/{cfg.model}")

                with c2:
                    if is_browser and mgr:
                        if has_tab:
                            ua_col, hc_col = st.columns([1, 1])
                            with ua_col:
                                if st.button("Unassign", key=f"ua_{name}", use_container_width=True):
                                    mgr.unassign(name); st.rerun()
                            with hc_col:
                                if st.button("⚡ Check", key=f"fhc_{name}", use_container_width=True,
                                             help="Fast tab health check — no prompt sent"):
                                    _set_agent_statuses({name: "Checking"})
                                    try:
                                        result = mgr.fast_health_check(name)
                                        st.session_state[f"hc_result_{name}"] = result
                                        if result.get("ok"):
                                            _set_agent_statuses({name: "Healthy"})
                                            st.session_state.pop(f"hc_err_{name}", None)
                                        else:
                                            reason = result.get("reason", "Unknown error")
                                            st.session_state[f"hc_err_{name}"] = reason
                                            _set_agent_statuses({name: "Failed"})
                                    except Exception as exc:
                                        st.session_state[f"hc_err_{name}"] = str(exc)
                                        _set_agent_statuses({name: "Failed"})
                                    st.rerun()
                            hc_err = st.session_state.pop(f"hc_err_{name}", None)
                            if hc_err:
                                st.error(hc_err)
                            # Persistent marker status from the last "⚡ Check" run --
                            # not re-checked on every render (Playwright round-trip
                            # per agent on every rerun was exactly the latency
                            # mistake active_agents()/list_agents() already fixed
                            # elsewhere; this only updates when the operator clicks).
                            last_check = st.session_state.get(f"hc_result_{name}")
                            if last_check:
                                marker = last_check.get("marker", "")
                                if last_check.get("marker_ok") is False:
                                    st.caption(f"🏷 Marker mismatch — tab marked '{marker}'")
                                elif marker:
                                    st.caption(f"🏷 Marker: {marker} ✓")
                                else:
                                    st.caption("🏷 Marker: unmarked (pre-existing tab, treated as OK)")
                                sc = last_check.get("semantic_count")
                                if sc is not None:
                                    st.caption(f"📡 Semantic count: {sc}")
                            if st.button("🔬 Full Check", key=f"fullhc_{name}", use_container_width=True,
                                         help="Sends 'health-ok' probe and verifies capture — slow"):
                                _set_agent_statuses({name: "Checking"})
                                try:
                                    result = mgr.full_health_check(name)
                                    if result.get("ok"):
                                        _set_agent_statuses({name: "Healthy"})
                                        st.session_state.pop(f"hc_err_{name}", None)
                                    else:
                                        reason = result.get("reason") or f"Response: {result.get('response','?')[:80]}"
                                        st.session_state[f"hc_err_{name}"] = f"Full Check: {reason}"
                                        _set_agent_statuses({name: "Failed"})
                                except Exception as exc:
                                    st.session_state[f"hc_err_{name}"] = f"Full Check error: {exc}"
                                    _set_agent_statuses({name: "Failed"})
                                st.rerun()
                        else:
                            site_key_override = getattr(cfg, "site_key", "")
                            site = site_for_agent(name, site_key_override)
                            is_claude = site and site.get("key") in ("claude_chat", "claude_code")
                            bc1, bc2 = st.columns(2)
                            with bc1:
                                if st.button("Find Tab", key=f"ft_{name}", use_container_width=True,
                                             type="primary" if is_claude else "secondary"):
                                    found, msg = find_tab_for_agent(mgr, name, cfg)
                                    if found:
                                        st.session_state.pop(f"tab_err_{name}", None)
                                        st.rerun()
                                    else:
                                        st.session_state[f"tab_err_{name}"] = msg
                                        st.rerun()
                            with bc2:
                                if st.button("Open Tab", key=f"ot_{name}", use_container_width=True):
                                    with st.spinner(f"Opening {name.upper()}…"):
                                        try:
                                            mgr.open_tab(name, site_key_override); st.rerun()
                                        except Exception as e:
                                            st.session_state[f"tab_err_{name}"] = str(e); st.rerun()
                            err = st.session_state.pop(f"tab_err_{name}", None)
                            if err:
                                st.error(err)
                            if is_claude and not has_tab:
                                if site["key"] == "claude_code":
                                    st.caption("💡 Open claude.ai/code in Chrome, then **Find Tab**")
                                else:
                                    st.caption("💡 Open claude.ai in Chrome, sign in, then **Find Tab**")
                    elif is_browser and not mgr:
                        st.caption("Connect Chrome first")

                with c3:
                    if st.button("Edit", key=f"e_{name}"):
                        st.session_state.edit_agent = name; st.rerun()
                with c4:
                    if st.button("Del", key=f"del_{name}"):
                        if mgr:
                            mgr.unassign(name)
                        del agents_cfg[name]; save_agents(agents_cfg); st.rerun()

    elif ea == "__new__":
        st.header("New Agent")
        new_name = st.text_input("Name", placeholder="e.g. gemini")
        new_mode = st.radio("Mode", ["api", "browser"], horizontal=True)

        if new_mode == "api":
            new_provider = st.selectbox("Provider", ["anthropic", "openai"])
            model_list   = ANTHROPIC_MODELS if new_provider == "anthropic" else OPENAI_MODELS
            new_model    = st.selectbox("Model", model_list)
        else:
            new_provider = "browser"
            new_model    = ""
            new_site_key = st.selectbox(
                "Browser site", ["(auto from name)", "claude_chat", "claude_code"],
                help='"claude_code" only matters for the literal claude.ai/code IDE view — '
                     "leave on auto for normal chat personas, even ones named "
                     '"claude_code_architect"/etc.',
            )
            new_site_key = "" if new_site_key == "(auto from name)" else new_site_key
            resolved = site_for_agent(new_name.strip(), new_site_key) if PLAYWRIGHT_AVAILABLE else None
            if resolved:
                st.caption(f"Will open: {resolved['url']}")

        new_prompt = st.text_area("System Prompt", height=200,
                                  placeholder="Describe this agent's personality and approach.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save", type="primary", use_container_width=True):
                slug = new_name.strip().lower().replace(" ", "_")
                if not slug:             st.warning("Enter a name.")
                elif slug in agents_cfg: st.warning(f"'{slug}' already exists.")
                else:
                    agents_cfg[slug] = AgentConfig(slug, new_provider, new_model,
                                                   new_prompt.strip(), new_mode,
                                                   site_key=new_site_key if new_mode == "browser" else "")
                    save_agents(agents_cfg)
                    st.session_state.edit_agent = None; st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True):
                st.session_state.edit_agent = None; st.rerun()

    else:
        cfg = agents_cfg.get(ea)
        if not cfg:
            st.error(f"Agent '{ea}' not found."); st.stop()

        st.header(f"Edit — {avatar(ea)} {ea.upper()}")
        upd_mode = st.radio("Mode", ["api", "browser"], horizontal=True,
                            index=0 if getattr(cfg, "mode", "api") == "api" else 1)

        if upd_mode == "api":
            upd_provider = st.selectbox("Provider", ["anthropic", "openai"],
                                        index=0 if cfg.provider == "anthropic" else 1)
            model_list   = ANTHROPIC_MODELS if upd_provider == "anthropic" else OPENAI_MODELS
            upd_model    = st.selectbox("Model", model_list,
                                        index=model_list.index(cfg.model) if cfg.model in model_list else 0)
        else:
            upd_provider = "browser"
            upd_model    = ""
            _site_opts   = ["(auto from name)", "claude_chat", "claude_code"]
            _cur_site_key = getattr(cfg, "site_key", "") or "(auto from name)"
            upd_site_key = st.selectbox(
                "Browser site", _site_opts,
                index=_site_opts.index(_cur_site_key) if _cur_site_key in _site_opts else 0,
                help='Set to "claude_code" only for an agent dedicated to driving the '
                     "literal claude.ai/code IDE view.",
            )
            upd_site_key = "" if upd_site_key == "(auto from name)" else upd_site_key

        upd_prompt = st.text_area("System Prompt", value=cfg.system_prompt, height=260)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save", type="primary", use_container_width=True):
                agents_cfg[ea] = AgentConfig(ea, upd_provider, upd_model,
                                             upd_prompt.strip(), upd_mode,
                                             site_key=upd_site_key if upd_mode == "browser" else "")
                save_agents(agents_cfg)
                st.session_state.edit_agent = None; st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True):
                st.session_state.edit_agent = None; st.rerun()

# ── SETTINGS ──────────────────────────────────────────────────────────────────

else:
    st.header("Settings")
    st.caption("Saved locally to `keys.json` — never leaves your machine.")

    st.subheader("Chrome Setup")
    with st.container(border=True):
        st.markdown("### Step 1 — Pre-authenticate (first time only)")
        st.markdown(
            "Launch Chrome **without** the debug port so Google OAuth runs in a clean browser "
            "(Google detects and blocks sign-in inside a debug-mode window):"
        )
        st.code(
            'Start-Process "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '-ArgumentList "--user-data-dir=C:\\relay-chrome-profile"',
            language="powershell",
        )
        st.markdown(
            "In that window, sign into **claude.ai, chatgpt.com, gemini.google.com, perplexity.ai** normally. "
            "Then close Chrome completely (all windows)."
        )

        st.divider()
        st.markdown("### Step 2 — Launch for Relay (every session)")
        st.markdown("Reopen Chrome with the debug port — same profile, so all logins are already there:")
        st.code(
            'Start-Process "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            '-ArgumentList "--remote-debugging-port=9222","--remote-allow-origins=*","--user-data-dir=C:\\relay-chrome-profile"',
            language="powershell",
        )
        st.markdown(
            "- Paste into **PowerShell** and press Enter\n"
            "- Sites should open already logged in — no re-auth needed\n"
            "- Go to **Agents** → **Connect to Chrome** → **Find Tab** for each agent\n\n"
            "> **If a Google session ever expires** while in debug mode, close Chrome, repeat Step 1 to re-auth, then Step 2 to relaunch."
        )
        st.caption(f"Relay connects to: `{CDP_URL}`  ·  Profile dir: `C:\\relay-chrome-profile`")

    st.subheader("API Keys")
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("**Anthropic**")
            ant = st.text_input("Anthropic key", value=saved_keys.get("ANTHROPIC_API_KEY", ""),
                                type="password", placeholder="sk-ant-...",
                                label_visibility="collapsed")
            st.success("Active") if os.environ.get("ANTHROPIC_API_KEY") else st.warning("Not set")
    with col2:
        with st.container(border=True):
            st.markdown("**OpenAI**")
            oai = st.text_input("OpenAI key", value=saved_keys.get("OPENAI_API_KEY", ""),
                                type="password", placeholder="sk-...",
                                label_visibility="collapsed")
            st.success("Active") if os.environ.get("OPENAI_API_KEY") else st.warning("Not set")

    st.subheader("GitHub")
    st.caption("One markdown file per topic — commits after every round. Paste the raw URL into any AI for instant context.")
    with st.container(border=True):
        gc1, gc2 = st.columns([3, 2])
        with gc1:
            gh_token = st.text_input("Personal Access Token", value=saved_keys.get("GITHUB_TOKEN", ""),
                                     type="password", placeholder="ghp_...",
                                     label_visibility="collapsed")
        with gc2:
            gh_repo = st.text_input("Repo name", value=saved_keys.get("GITHUB_REPO", "relay-conversations"),
                                    label_visibility="collapsed")
        repo = get_github_repo()
        if repo:
            st.success(f"Connected — [{repo.full_name}]({repo.html_url})")
        elif saved_keys.get("GITHUB_TOKEN"):
            st.warning("Could not connect — check token and repo name")
        else:
            st.caption("Not configured")

    if st.button("Save", type="primary"):
        new_keys = {
            "ANTHROPIC_API_KEY": ant.strip(),
            "OPENAI_API_KEY":    oai.strip(),
            "GITHUB_TOKEN":      gh_token.strip(),
            "GITHUB_REPO":       gh_repo.strip(),
        }
        save_keys(new_keys)
        apply_keys(new_keys)
        st.cache_resource.clear()
        st.success("Saved.")
        st.rerun()
