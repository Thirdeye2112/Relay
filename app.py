#!/usr/bin/env python3
"""
relay/app.py  —  Streamlit UI for relay agent debates
Run: streamlit run app.py
"""

import os, sys, json, datetime, threading
import streamlit as st
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from relay import (
    load_agents, load_session, load_meta, save_meta,
    append_message, Message, build_system,
    SESSIONS_DIR, AGENTS_FILE, AgentConfig,
)
try:
    from browser_relay import BrowserWorker, site_for_agent, SITES
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Relay", page_icon="⚡", layout="wide")

# ── Load & apply saved API keys (runs on every Streamlit rerun) ────────────────

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
        if v:
            os.environ[k] = v

saved_keys = load_keys()
apply_keys(saved_keys)

# ── GitHub integration ─────────────────────────────────────────────────────────

try:
    from github_relay import (
        get_repo as _gh_get_repo, append_round as _gh_append_round,
        commit_async, html_url as _gh_html_url, raw_url as _gh_raw_url,
        topic_to_path,
    )
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False

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

# ── Agent registry ─────────────────────────────────────────────────────────────

def reload_agents():
    return load_agents()

agents_cfg  = reload_agents()
agent_names = list(agents_cfg.keys())

_EMOJIS = ["🔵", "🔴", "🟢", "🟡", "🟠", "🟣"]
def avatar(name: str) -> str:
    all_names = list(agents_cfg.keys())
    idx = all_names.index(name) if name in all_names else 0
    return _EMOJIS[idx % len(_EMOJIS)]

def provider_key(provider: str) -> str:
    return "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"

def agent_active(cfg) -> bool:
    if getattr(cfg, "mode", "api") == "browser":
        return bool(get_worker(cfg.name))
    return bool(os.environ.get(provider_key(cfg.provider), ""))

def active_agents() -> dict:
    return {n: c for n, c in agents_cfg.items() if agent_active(c)}

# ── Save agents back to agents.yaml ───────────────────────────────────────────

def save_agents(agents: dict) -> None:
    import yaml
    data = {"agents": {
        name: {"provider": c.provider, "model": c.model, "system_prompt": c.system_prompt}
        for name, c in agents.items()
    }}
    with open(AGENTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Streaming helpers ──────────────────────────────────────────────────────────

def _stream_anthropic(model: str, messages: list[Message]):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        st.error("ANTHROPIC_API_KEY not set — add it in Settings."); st.stop()
    client = anthropic.Anthropic(api_key=key)
    system   = next((m.content for m in messages if m.role == "system"), "")
    api_msgs = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    with client.messages.stream(model=model, max_tokens=2048, system=system, messages=api_msgs) as s:
        for chunk in s.text_stream:
            yield chunk

def _stream_openai(model: str, messages: list[Message]):
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        st.error("OPENAI_API_KEY not set — add it in Settings."); st.stop()
    client = OpenAI(api_key=key)
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": m.role, "content": m.content} for m in messages],
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
    else:
        st.error(f"Unknown provider: {cfg.provider}"); st.stop()

def _api_error_msg(e: Exception) -> str:
    msg = str(e)
    if "credit balance" in msg.lower() or "insufficient" in msg.lower():
        return "Credit balance too low — top up at console.anthropic.com/billing (note: Claude.ai Plus ≠ API credits)"
    if "invalid api key" in msg.lower() or "authentication" in msg.lower() or "401" in msg:
        return "Invalid API key — check the Settings tab"
    if "rate limit" in msg.lower() or "429" in msg:
        return "Rate limit hit — wait a moment and try again"
    return f"API error: {msg[:200]}"

# ── Session helpers ────────────────────────────────────────────────────────────

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
    t = m.get("type", "")
    if t == "group_chat":
        agents_in = ", ".join(a.upper() for a in m.get("agents", []))
        topic = m.get("topic", "Group Chat")[:24]
        return f"[GROUP] {agents_in} — {topic}"
    pair  = " vs ".join(a.upper() for a in m.get("agents", []))
    topic = m.get("topic", "")[:24]
    return f"{pair} — {topic}"

# ── Group chat display log ─────────────────────────────────────────────────────

def display_log_path(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "display.jsonl"

def append_display(session_id: str, event: dict) -> None:
    path = display_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def render_display_log(session_id: str) -> None:
    path = display_log_path(session_id)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev["type"] == "user":
            with st.chat_message("user", avatar="🧑"):
                st.markdown(ev["content"])
        elif ev["type"] == "reply":
            name = ev["agent"]
            with st.chat_message(name, avatar=avatar(name)):
                st.markdown(f"**{name.upper()}**\n\n{ev['content']}")

# ── Transcript import ──────────────────────────────────────────────────────────

_USER_ALIASES = {"you", "user", "human", "me"}

def _parse_turns(text: str, agent_name: str) -> list[tuple[str, str]]:
    """
    Parse one agent's own conversation transcript.
    Lines prefixed with You/User/Human/Me → role 'user'.
    Lines prefixed with the agent's name (any casing/spacing) → role 'assistant'.
    Unlabeled continuation lines belong to the current turn.
    """
    aliases = {agent_name.lower(), agent_name.lower().replace("_", " "),
               agent_name.lower().replace("_", ""), agent_name.lower().replace("_", "-")}

    turns = []
    current_role  = None
    current_lines = []

    def flush():
        if current_role and current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                turns.append((current_role, content))

    for line in text.splitlines():
        colon_pos = line.find(":")
        if colon_pos > 0:
            prefix = line[:colon_pos].strip().lower()
            rest   = line[colon_pos + 1:].strip()
            if prefix in _USER_ALIASES:
                flush(); current_role = "user"; current_lines = [rest]
                continue
            if prefix in aliases:
                flush(); current_role = "assistant"; current_lines = [rest]
                continue
        # Unlabeled line — if we haven't seen a speaker yet, treat as assistant
        if current_role is None:
            current_role = "assistant"
        current_lines.append(line)

    flush()
    return turns

def seed_agent_history(session_id: str, agent_name: str,
                       turns: list[tuple[str, str]]) -> None:
    """Write one agent's own prior conversation into its JSONL history."""
    for role, content in turns:
        append_message(session_id, agent_name, Message(role, content))

# ── Delete session ─────────────────────────────────────────────────────────────

def delete_session(session_id: str) -> None:
    import shutil
    path = SESSIONS_DIR / session_id
    if path.exists():
        shutil.rmtree(path)

# ── Group chat round runner ────────────────────────────────────────────────────

def run_group_round(session_id: str, participants: dict, user_msg: str) -> None:
    """Send user_msg to all active participants, stream replies, cross-inject."""
    # Record user message in display log
    append_display(session_id, {"type": "user", "content": user_msg})

    # Add user message to every agent's API history
    for name in participants:
        append_message(session_id, name, Message("user", user_msg))

    replies = {}

    # Each agent responds — API agents stream live, browser agents show a spinner
    for name, cfg in participants.items():
        with st.chat_message(name, avatar=avatar(name)):
            st.markdown(f"**{name.upper()}**")
            try:
                if cfg.mode == "browser":
                    worker = get_worker(name)
                    if not worker:
                        st.error(f"{name.upper()} browser is not open — launch it in the Agents tab.")
                        continue
                    with st.spinner(f"{name.upper()} is responding in browser…"):
                        reply = worker.send_message(user_msg)
                    st.markdown(reply)
                else:
                    history = load_session(session_id, name)
                    reply   = st.write_stream(stream_agent(cfg, history))
            except Exception as e:
                err = _api_error_msg(e) if cfg.mode != "browser" else str(e)
                st.error(err)
                continue
        replies[name] = reply
        append_display(session_id, {"type": "reply", "agent": name, "content": reply})
        append_message(session_id, name, Message("assistant", reply))

    # Cross-inject: every agent gets all other agents' replies as user messages
    for name in participants:
        for other, reply in replies.items():
            if other != name:
                append_message(session_id, name, Message("user", f"[{other.upper()}]: {reply}"))

    # Commit round to GitHub (background — never blocks the UI)
    if replies:
        repo = get_github_repo()
        if repo:
            meta  = load_meta(session_id)
            topic = meta.get("topic", "conversation")
            commit_async(
                _gh_append_round, repo, topic,
                list(participants.keys()), user_msg, replies,
            )

# ── Debate transcript renderer ─────────────────────────────────────────────────

def render_transcript(session_id: str, a_name: str, b_name: str) -> None:
    history   = load_session(session_id, a_name)
    b_prefix  = f"[{b_name.upper()}]:"
    round_num = 0
    for msg in history:
        if msg.role == "system":
            continue
        if msg.role == "user":
            if msg.content.startswith(b_prefix):
                body = msg.content[len(b_prefix):].strip()
                with st.chat_message(b_name, avatar=avatar(b_name)):
                    st.markdown(f"**{b_name.upper()}**\n\n{body}")
            elif msg.content.startswith("[MODERATOR]:"):
                st.info(f"🧑 **MODERATOR:** {msg.content[12:].strip()}")
            else:
                st.caption(f"📌 {msg.content.split(chr(10))[0]}")
        elif msg.role == "assistant":
            round_num += 1
            st.caption(f"── Round {round_num}")
            with st.chat_message(a_name, avatar=avatar(a_name)):
                st.markdown(f"**{a_name.upper()}**\n\n{msg.content}")

# ── Debate round runner ────────────────────────────────────────────────────────

def run_debate_rounds(session_id: str, a_name: str, b_name: str, meta: dict, rounds: int) -> None:
    agent_a = agents_cfg[a_name]
    agent_b = agents_cfg[b_name]
    start   = meta["rounds_completed"] + 1
    for r in range(start, start + rounds):
        st.caption(f"── Round {r}")
        try:
            with st.chat_message(a_name, avatar=avatar(a_name)):
                st.markdown(f"**{a_name.upper()}**")
                reply_a = st.write_stream(stream_agent(agent_a, load_session(session_id, a_name)))
            append_message(session_id, a_name, Message("assistant", reply_a))
            append_message(session_id, b_name, Message("user", f"[{a_name.upper()}]: {reply_a}"))
            with st.chat_message(b_name, avatar=avatar(b_name)):
                st.markdown(f"**{b_name.upper()}**")
                reply_b = st.write_stream(stream_agent(agent_b, load_session(session_id, b_name)))
            append_message(session_id, b_name, Message("assistant", reply_b))
            append_message(session_id, a_name, Message("user", f"[{b_name.upper()}]: {reply_b}"))
            meta["rounds_completed"] = r
            save_meta(session_id, meta)
            # Commit debate round to GitHub (background)
            repo = get_github_repo()
            if repo:
                commit_async(
                    _gh_append_round, repo, meta.get("topic", "debate"),
                    [a_name, b_name], None,
                    {a_name: reply_a, b_name: reply_b},
                )
        except Exception as e:
            st.error(_api_error_msg(e))
            return

# ── Session state ──────────────────────────────────────────────────────────────

if "active_session"  not in st.session_state: st.session_state.active_session  = None
if "edit_agent"      not in st.session_state: st.session_state.edit_agent      = None
if "browser_workers" not in st.session_state: st.session_state.browser_workers = {}
# {agent_name: BrowserWorker}  — persists across Streamlit reruns within a session

def get_worker(name: str) -> "BrowserWorker | None":
    w = st.session_state.browser_workers.get(name)
    return w if (w and w.alive) else None

def launch_worker(name: str) -> None:
    existing = st.session_state.browser_workers.get(name)
    if existing and existing.alive:
        return
    worker = BrowserWorker(name)
    st.session_state.browser_workers[name] = worker

def close_worker(name: str) -> None:
    w = st.session_state.browser_workers.pop(name, None)
    if w:
        w.close()

# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.title("⚡ Relay")
    mode = st.radio("Mode", ["Chat", "Debates", "Agents", "Settings"],
                    horizontal=True, label_visibility="collapsed")
    st.divider()

    active = active_agents()

    # ── CHAT sidebar ───────────────────────────────────────────────────────────
    if mode == "Chat":
        sessions = [s for s in all_sessions() if load_meta(s.name).get("type") == "group_chat"]
        chosen_chat = None
        if sessions:
            st.subheader("Conversations")
            for s in sessions:
                col_label, col_del = st.columns([5, 1])
                with col_label:
                    is_sel = st.session_state.active_session == s.name
                    label  = ("▶ " if is_sel else "") + session_label(s.name)
                    if st.button(label, use_container_width=True, key=f"sel_{s.name}"):
                        st.session_state.active_session = s.name
                        st.rerun()
                with col_del:
                    if st.button("🗑", key=f"del_{s.name}", help="Delete this conversation"):
                        if st.session_state.active_session == s.name:
                            st.session_state.active_session = None
                        delete_session(s.name)
                        st.rerun()
            chosen_chat = st.session_state.active_session
        else:
            st.caption("No conversations yet.")
            st.session_state.active_session = None

        st.divider()
        if not active:
            st.warning("Add API keys in **Settings** first.")
        else:
            st.subheader("New Conversation")
            for name, cfg in active.items():
                st.caption(f"{avatar(name)} {name.upper()}  ({cfg.model})")
            topic = st.text_input("Topic / context", placeholder="Optional — e.g. software architecture")

            with st.expander("📋 Import prior conversations"):
                st.caption(
                    "Paste each AI's existing conversation so it remembers what it said.\n"
                    "Label your lines **You:** (or User:). "
                    "The AI's lines can be labeled or unlabeled — either works."
                )
                per_agent_imports = {}
                for name in active:
                    per_agent_imports[name] = st.text_area(
                        f"{avatar(name)} {name.upper()} conversation history",
                        height=140,
                        key=f"import_{name}",
                        placeholder=f"You: ...\n{name.upper()}: ...\nYou: ...\n{name.upper()}: ...",
                    )
            new_chat_btn = st.button("＋ Start Conversation", type="primary", use_container_width=True)

    # ── DEBATES sidebar ────────────────────────────────────────────────────────
    elif mode == "Debates":
        sessions = [s for s in all_sessions() if load_meta(s.name).get("type") == "debate"]
        if sessions:
            st.subheader("Sessions")
            for s in sessions:
                col_label, col_del = st.columns([5, 1])
                with col_label:
                    is_sel = st.session_state.active_session == s.name
                    label  = ("▶ " if is_sel else "") + session_label(s.name)
                    if st.button(label, use_container_width=True, key=f"dsel_{s.name}"):
                        st.session_state.active_session = s.name
                        st.rerun()
                with col_del:
                    if st.button("🗑", key=f"ddel_{s.name}", help="Delete this session"):
                        if st.session_state.active_session == s.name:
                            st.session_state.active_session = None
                        delete_session(s.name)
                        st.rerun()
        else:
            st.caption("No debates yet.")
            st.session_state.active_session = None

        st.divider()
        st.subheader("New Debate")
        if len(active) < 2:
            st.warning("Need at least 2 active agents. Add keys in **Settings**.")
        else:
            active_names = list(active.keys())
            col1, col2  = st.columns(2)
            with col1:
                new_a = st.selectbox("Agent A", active_names)
            with col2:
                new_b = st.selectbox("Agent B", [n for n in active_names if n != new_a])
            new_topic  = st.text_input("Topic", placeholder="e.g. monorepo vs polyrepo")
            new_rounds = st.number_input("Rounds", 1, 20, 5)
            start_btn  = st.button("▶ Start Debate", type="primary", use_container_width=True)

    # ── AGENTS sidebar ─────────────────────────────────────────────────────────
    elif mode == "Agents":
        st.subheader("Agents")
        for name, cfg in agents_cfg.items():
            is_on = agent_active(cfg)
            label = f"{avatar(name)} {name.upper()}  {'✓' if is_on else '○'}"
            if st.button(label, use_container_width=True, key=f"sel_{name}"):
                st.session_state.edit_agent = name
        st.divider()
        if st.button("＋ New Agent", type="primary", use_container_width=True):
            st.session_state.edit_agent = "__new__"

    # ── SETTINGS sidebar ───────────────────────────────────────────────────────
    else:
        st.subheader("API Keys")
        ant_active = bool(os.environ.get("ANTHROPIC_API_KEY"))
        oai_active = bool(os.environ.get("OPENAI_API_KEY"))
        st.caption(f"Anthropic  {'✓ active' if ant_active else '○ not set'}")
        st.caption(f"OpenAI     {'✓ active' if oai_active else '○ not set'}")

# ══════════════════════════════════════════════════════════════════════════════
# Main area
# ══════════════════════════════════════════════════════════════════════════════

# ── SETTINGS ──────────────────────────────────────────────────────────────────

if mode == "Settings":
    st.header("Settings")
    st.caption("All keys are saved locally to `keys.json` and never leave your machine.")

    # ── API Keys ───────────────────────────────────────────────────────────────
    st.subheader("API Keys")
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("**Anthropic**")
            st.caption("Used by: " + (", ".join(
                n.upper() for n, c in agents_cfg.items() if c.provider == "anthropic"
            ) or "no agents yet"))
            ant_key = st.text_input("ANTHROPIC_API_KEY",
                value=saved_keys.get("ANTHROPIC_API_KEY", ""),
                type="password", placeholder="sk-ant-...",
                label_visibility="collapsed")
            st.success("Active") if os.environ.get("ANTHROPIC_API_KEY") else st.warning("Not set")

    with col2:
        with st.container(border=True):
            st.markdown("**OpenAI**")
            st.caption("Used by: " + (", ".join(
                n.upper() for n, c in agents_cfg.items() if c.provider == "openai"
            ) or "no agents yet"))
            oai_key = st.text_input("OPENAI_API_KEY",
                value=saved_keys.get("OPENAI_API_KEY", ""),
                type="password", placeholder="sk-...",
                label_visibility="collapsed")
            st.success("Active") if os.environ.get("OPENAI_API_KEY") else st.warning("Not set")

    # ── GitHub ─────────────────────────────────────────────────────────────────
    st.subheader("GitHub")
    st.caption(
        "Every conversation is committed to this repo as one continuous markdown file per topic. "
        "Share the raw URL with any AI to give it full context instantly."
    )
    with st.container(border=True):
        gh_col1, gh_col2 = st.columns([3, 2])
        with gh_col1:
            gh_token = st.text_input("Personal Access Token",
                value=saved_keys.get("GITHUB_TOKEN", ""),
                type="password", placeholder="ghp_...",
                help="github.com → Settings → Developer settings → Personal access tokens → repo scope")
        with gh_col2:
            gh_repo = st.text_input("Repository name",
                value=saved_keys.get("GITHUB_REPO", "relay-conversations"),
                placeholder="relay-conversations",
                help="Will be created automatically if it doesn't exist yet")
        repo = get_github_repo()
        if repo:
            st.success(f"Connected — [{repo.full_name}]({repo.html_url})")
        elif saved_keys.get("GITHUB_TOKEN"):
            st.warning("Could not connect — check token and repo name")
        else:
            st.caption("Not configured — conversations save locally only")

    if st.button("Save All Settings", type="primary"):
        new_keys = {
            "ANTHROPIC_API_KEY": ant_key.strip(),
            "OPENAI_API_KEY":    oai_key.strip(),
            "GITHUB_TOKEN":      gh_token.strip(),
            "GITHUB_REPO":       gh_repo.strip(),
        }
        save_keys(new_keys)
        apply_keys(new_keys)
        st.cache_resource.clear()  # force repo reconnect
        st.success("Saved.")
        st.rerun()

    # ── Active Agents ──────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Active Agents")
    if not agents_cfg:
        st.caption("No agents defined. Go to the Agents tab to add some.")
    else:
        for name, cfg in agents_cfg.items():
            is_on = agent_active(cfg)
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    st.markdown(f"{avatar(name)} **{name.upper()}**  —  {cfg.mode} / {cfg.provider} / {cfg.model}")
                with c2:
                    st.success("Active") if is_on else st.warning("No key")

# ── AGENTS ────────────────────────────────────────────────────────────────────

elif mode == "Agents":
    ANTHROPIC_MODELS = [
        "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001",
        "claude-3-5-sonnet-20241022", "claude-3-haiku-20240307",
    ]
    OPENAI_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]

    ea = st.session_state.edit_agent

    if ea is None:
        st.header("Agents")
        if not agents_cfg:
            st.info("No agents yet. Click **＋ New Agent** in the sidebar.")

        for name, cfg in agents_cfg.items():
            is_browser = cfg.mode == "browser"
            worker     = get_worker(name) if is_browser else None
            is_on      = agent_active(cfg)

            with st.container(border=True):
                c1, cbtn, cedit, cdel = st.columns([5, 2, 1, 1])
                with c1:
                    st.markdown(f"### {avatar(name)} {name.upper()}")
                    if is_browser:
                        site = site_for_agent(name)
                        site_label = site["url"] if site else "unknown site"
                        if worker:
                            info = worker.status()
                            st.caption(f"🌐 Browser · {info.get('title', site_label)} · ✓ open")
                        else:
                            st.caption(f"🌐 Browser · {site_label} · ○ closed")
                    else:
                        key_ok = bool(os.environ.get(provider_key(cfg.provider), ""))
                        st.caption(
                            f"API · {cfg.provider} / {cfg.model} · "
                            f"{'✓ key set' if key_ok else '○ no key — add in Settings'}"
                        )
                    st.markdown(cfg.system_prompt[:200] + ("…" if len(cfg.system_prompt) > 200 else ""))

                with cbtn:
                    if is_browser:
                        if not PLAYWRIGHT_AVAILABLE:
                            st.caption("playwright not installed")
                        elif worker:
                            if st.button("Close browser", key=f"close_{name}", use_container_width=True):
                                close_worker(name); st.rerun()
                        else:
                            if st.button("Launch browser", key=f"launch_{name}",
                                         type="primary", use_container_width=True):
                                with st.spinner(f"Opening {name.upper()} browser…"):
                                    try:
                                        launch_worker(name)
                                    except Exception as e:
                                        st.error(str(e))
                                st.rerun()

                with cedit:
                    if st.button("Edit", key=f"edit_{name}"):
                        st.session_state.edit_agent = name; st.rerun()
                with cdel:
                    if st.button("Del", key=f"del_{name}"):
                        close_worker(name)
                        del agents_cfg[name]; save_agents(agents_cfg); st.rerun()

        if not PLAYWRIGHT_AVAILABLE:
            st.divider()
            st.warning(
                "**Playwright not installed** — browser agents won't work.\n\n"
                "Run once:\n```\npip install playwright\nplaywright install chromium\n```"
            )

    elif ea == "__new__":
        st.header("New Agent")
        new_name  = st.text_input("Name", placeholder="e.g. gemini")
        new_mode  = st.radio("Mode", ["api", "browser"], horizontal=True,
                             help="API: uses an API key and model. Browser: controls a real browser tab.")

        if new_mode == "api":
            new_provider = st.selectbox("Provider", ["anthropic", "openai"])
            model_list   = ANTHROPIC_MODELS if new_provider == "anthropic" else OPENAI_MODELS
            new_model    = st.selectbox("Model", model_list)
        else:
            known_sites = list(SITES.keys()) if PLAYWRIGHT_AVAILABLE else []
            st.caption(f"Supported sites: {', '.join(known_sites) or 'playwright not installed'}")
            new_provider = "browser"
            new_model    = ""

        new_prompt = st.text_area("System Prompt", height=220,
                                  placeholder="Describe this agent's personality and debate style.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save", type="primary", use_container_width=True):
                slug = new_name.strip().lower().replace(" ", "_")
                if not slug:             st.warning("Enter a name.")
                elif slug in agents_cfg: st.warning(f"'{slug}' already exists.")
                else:
                    agents_cfg[slug] = AgentConfig(
                        slug, new_provider, new_model, new_prompt.strip(), new_mode
                    )
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
                            index=["api", "browser"].index(cfg.mode if cfg.mode in ["api","browser"] else "api"))

        if upd_mode == "api":
            upd_provider = st.selectbox("Provider", ["anthropic", "openai"],
                                        index=["anthropic", "openai"].index(cfg.provider)
                                        if cfg.provider in ["anthropic","openai"] else 0)
            model_list = ANTHROPIC_MODELS if upd_provider == "anthropic" else OPENAI_MODELS
            upd_model  = st.selectbox("Model", model_list,
                                      index=model_list.index(cfg.model) if cfg.model in model_list else 0)
        else:
            upd_provider = "browser"
            upd_model    = ""
            if PLAYWRIGHT_AVAILABLE:
                site = site_for_agent(ea)
                st.caption(f"Will open: {site['url'] if site else 'unknown — name must match a known site'}")

        upd_prompt = st.text_area("System Prompt", value=cfg.system_prompt, height=260)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save Changes", type="primary", use_container_width=True):
                agents_cfg[ea] = AgentConfig(ea, upd_provider, upd_model, upd_prompt.strip(), upd_mode)
                save_agents(agents_cfg)
                st.session_state.edit_agent = None; st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True):
                st.session_state.edit_agent = None; st.rerun()

# ── CHAT ──────────────────────────────────────────────────────────────────────

elif mode == "Chat":
    if not active:
        st.info("No active agents. Go to **Settings** and add your API keys.")

    elif "new_chat_btn" in dir() and new_chat_btn:
        # Start a new group conversation with ALL active agents
        ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        sid = f"groupchat-{ts}"
        opening_topic = topic.strip() or "Open conversation"
        # Parse per-agent imports first so we can record them in meta
        imports = {}
        pai = per_agent_imports if "per_agent_imports" in dir() else {}
        for name in active:
            raw = pai.get(name, "").strip()
            if raw:
                imports[name] = _parse_turns(raw, name)

        meta = {
            "session_id":      sid, "type": "group_chat",
            "topic":           opening_topic,
            "agents":          list(active.keys()),
            "imported_agents": list(imports.keys()),
            "created":         datetime.datetime.now().isoformat(),
        }
        save_meta(sid, meta)

        has_imports = bool(imports)

        # Seed each agent's history: system prompt, then their own prior conversation
        for name, cfg in active.items():
            others = [n for n in active if n != name]
            prior_note = (
                "\n\nYou have prior conversation history loaded below. "
                "You are now continuing that conversation in a group setting with "
                + ", ".join(o.upper() for o in others) + ".\n"
                if name in imports else ""
            )
            sys_prompt = (
                f"You are {name.upper()}. You are in a group conversation with "
                f"{', '.join(o.upper() for o in others)}.\n"
                f"Topic: {opening_topic}\n\n"
                f"When others speak, their messages appear as [NAME]: ...\n"
                f"Engage directly with what was said. Be concise and substantive."
                f"{prior_note}\n\n"
            ) + cfg.system_prompt
            append_message(sid, name, Message("system", sys_prompt))

            # Seed this agent's own prior conversation with correct roles
            if name in imports:
                seed_agent_history(sid, name, imports[name])

        st.session_state.active_session = sid
        st.rerun()

    elif st.session_state.active_session:
        sid  = st.session_state.active_session
        meta = load_meta(sid)
        if not meta or meta.get("type") != "group_chat":
            st.session_state.active_session = None; st.rerun()

        session_agents = meta.get("agents", [])
        participants   = {n: agents_cfg[n] for n in session_agents if n in agents_cfg and agent_active(agents_cfg[n])}
        missing        = [n for n in session_agents if n not in participants]

        # Header
        names_str = "  ·  ".join(f"{avatar(n)} {n.upper()}" for n in session_agents)
        st.header(names_str)
        st.caption(f"**{meta.get('topic', '')}**")

        if missing:
            st.warning(f"No key for: {', '.join(n.upper() for n in missing)} — they'll sit this one out.")

        # Show prior-history badges if any agent had imported history seeded
        imported_agents = meta.get("imported_agents", [])
        if imported_agents:
            st.caption(
                "📋 Prior history loaded for: "
                + ", ".join(n.upper() for n in imported_agents)
                + " — agents are continuing from where they left off."
            )

        # GitHub link — show before transcript so it's always visible
        gh_repo = get_github_repo()
        if gh_repo:
            topic    = meta.get("topic", "conversation")
            gh_link  = _gh_html_url(gh_repo, topic)
            raw_link = _gh_raw_url(gh_repo, topic)
            st.caption(
                f"📎 [View thread on GitHub]({gh_link})  ·  "
                f"[Raw URL for agents]({raw_link})"
            )

        # Render live conversation history
        render_display_log(sid)

        # Chat input — pinned to bottom
        if not participants:
            st.error("None of the agents in this session have active keys.")
        else:
            if user_input := st.chat_input("Message all agents…"):
                with st.chat_message("user", avatar="🧑"):
                    st.markdown(user_input)
                run_group_round(sid, participants, user_input)
                st.rerun()

    else:
        st.markdown("## ⚡ Chat")
        st.markdown("All active agents respond to each message and read each other's replies.")
        st.markdown("Click **＋ New Conversation** in the sidebar to start.")

# ── DEBATES ───────────────────────────────────────────────────────────────────

elif mode == "Debates":
    no_active = len(active) < 2
    has_start = not no_active and "start_btn" in dir() and start_btn

    if no_active:
        st.info("Need at least 2 active agents. Go to **Settings** and add your API keys.")

    elif has_start:
        if not new_topic.strip():
            st.warning("Enter a topic first."); st.stop()
        ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        sid = f"debate-{new_a}-{new_b}-{ts}"
        meta = {
            "session_id": sid, "type": "debate",
            "topic": new_topic.strip(), "agents": [new_a, new_b],
            "rounds_completed": 0, "created": datetime.datetime.now().isoformat(),
        }
        save_meta(sid, meta)
        append_message(sid, new_a, Message("system", build_system(agents_cfg[new_a], new_b, new_topic)))
        append_message(sid, new_b, Message("system", build_system(agents_cfg[new_b], new_a, new_topic)))
        opening = f'Topic: "{new_topic.strip()}"\n\nGive your opening position.'
        append_message(sid, new_a, Message("user", opening))
        append_message(sid, new_b, Message("user", opening))
        st.session_state.active_session = sid
        st.header(f"{avatar(new_a)} {new_a.upper()}  vs  {avatar(new_b)} {new_b.upper()}")
        st.caption(f"**{new_topic.strip()}**")
        run_debate_rounds(sid, new_a, new_b, meta, new_rounds)
        st.rerun()

    elif st.session_state.active_session:
        sid  = st.session_state.active_session
        meta = load_meta(sid)
        if not meta:
            st.error(f"Session '{sid}' not found."); st.stop()

        a_name = meta["agents"][0]
        b_name = meta["agents"][1]
        topic  = meta["topic"]

        st.header(f"{avatar(a_name)} {a_name.upper()}  vs  {avatar(b_name)} {b_name.upper()}")
        st.caption(f"**{topic}**  ·  {meta['rounds_completed']} rounds completed")
        render_transcript(sid, a_name, b_name)
        st.divider()

        col_resume, _, col_inject = st.columns([2, 1, 3])
        with col_resume:
            st.subheader("Continue")
            extra = st.number_input("Rounds to add", 1, 20, 3, key="extra_rounds")
            if st.button("▶ Resume", type="primary", use_container_width=True):
                run_debate_rounds(sid, a_name, b_name, meta, extra); st.rerun()

        with col_inject:
            st.subheader("Inject Message")
            st.caption("Goes into one agent's context before the next round.")
            ic1, ic2 = st.columns([1, 3])
            with ic1:
                inj_agent = st.selectbox("Agent", [a_name, b_name], label_visibility="collapsed")
            with ic2:
                inj_msg = st.text_input("Message", placeholder="e.g. What about the cost?",
                                        label_visibility="collapsed")
            if st.button("💬 Inject & Run 1 Round", use_container_width=True):
                if inj_msg.strip():
                    append_message(sid, inj_agent, Message("user", f"[MODERATOR]: {inj_msg.strip()}"))
                    run_debate_rounds(sid, a_name, b_name, meta, 1); st.rerun()

    else:
        st.markdown("## ⚡ Debates")
        st.markdown("Pick two agents to debate a topic, round by round.")
        st.markdown("Each agent reads the other's responses before replying.")
        st.markdown("Start one in the sidebar.")
