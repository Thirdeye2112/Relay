#!/usr/bin/env python3
"""relay/app.py — Run: python -m streamlit run app.py"""

import os, sys, json, datetime
from typing import Optional
import streamlit as st
from pathlib import Path

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
    site_for_agent = _br_mod.site_for_agent
    SITES          = _br_mod.SITES
    CDP_URL        = _br_mod.CDP_URL
    PLAYWRIGHT_AVAILABLE = True
    PLAYWRIGHT_ERROR = None
except Exception as _pw_err:
    PLAYWRIGHT_AVAILABLE = False
    PLAYWRIGHT_ERROR = str(_pw_err)
    def site_for_agent(name): return None  # noqa: E704
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

def agent_active(cfg) -> bool:
    if getattr(cfg, "mode", "api") == "browser":
        m = get_manager()
        return bool(m and m.has_agent(cfg.name))
    key = "ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"
    return bool(os.environ.get(key, ""))

def close_worker(name: str) -> None:
    m = get_manager()
    if m:
        m.unassign(name)

def active_agents() -> dict:
    return {n: c for n, c in agents_cfg.items() if agent_active(c)}

def save_agents(agents: dict) -> None:
    import yaml
    data = {"agents": {
        name: {
            "provider":      c.provider,
            "model":         c.model,
            "mode":          getattr(c, "mode", "api"),
            "system_prompt": c.system_prompt,
        }
        for name, c in agents.items()
    }}
    with open(AGENTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Streaming ─────────────────────────────────────────────────────────────────

def _stream_anthropic(model: str, messages: list[Message]):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        st.error("ANTHROPIC_API_KEY not set — add it in Settings."); st.stop()
    client   = anthropic.Anthropic(api_key=key)
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
    stream = OpenAI(api_key=key).chat.completions.create(
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

# claude.ai/code is Claude Code's IDE web view, not the chat composer --
# "claude.ai" is a substring of that URL too, so the plain url_match check
# below would happily hand a Claude Code tab to a chat-persona agent (e.g.
# claude_code_architect/claude_code_pragmatist are chat personas, not meant
# to drive the actual Claude Code product UI). None of the input selectors
# in SITES["claude"] exist on that page, so a tab assigned there fails with
# "Input box not found" every single time -- exclude it from auto-matching.
def _is_excluded_tab(url: str) -> bool:
    return "claude.ai/code" in url

def auto_assign_tabs(m: "BrowserManager") -> None:
    """Scan open tabs and assign each browser agent to the first matching tab."""
    tabs = m.scan_tabs()
    used: set[int] = set()
    for name, cfg in agents_cfg.items():
        if getattr(cfg, "mode", "api") != "browser":
            continue
        if m.has_agent(name):
            continue
        site = site_for_agent(name)
        if not site:
            continue
        for i, tab in enumerate(tabs):
            if _is_excluded_tab(tab["url"]):
                continue
            if site["url_match"] in tab["url"] and i not in used:
                try:
                    m.assign_tab(name, i)
                    used.add(i)
                except Exception:
                    pass
                break

def find_tab_for_agent(m: "BrowserManager", agent_name: str) -> str | None:
    """Find an open tab matching this agent and assign it. Returns tab URL or None."""
    tabs = m.scan_tabs()
    site = site_for_agent(agent_name)
    if not site:
        return None
    for i, tab in enumerate(tabs):
        if _is_excluded_tab(tab["url"]):
            continue
        if site["url_match"] in tab["url"]:
            try:
                m.assign_tab(agent_name, i)
                return tab["url"]
            except Exception:
                continue
    return None

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

# ── Display log ───────────────────────────────────────────────────────────────

def display_path(sid: str) -> Path:
    return SESSIONS_DIR / sid / "display.jsonl"

def append_display(sid: str, event: dict) -> None:
    p = display_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def render_display(sid: str) -> None:
    p = display_path(sid)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
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
        elif ev["type"] == "synthesis":
            with st.chat_message("synthesis", avatar="🔮"):
                st.markdown(f"**SYNTHESIS**\n\n{ev['content']}")

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

def run_synthesis(sid: str, user_msg: str, replies: dict) -> None:
    """Synthesize the round via Anthropic API directly (no agent-mode dependency)."""
    if not replies:
        return
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return  # silently skip -- user hasn't set a key

    prompt = _build_synthesis_prompt(user_msg, replies)
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
            st.error(_api_error(e))
            return

    append_display(sid, {"type": "synthesis", "content": synthesis})

# ── Group round ───────────────────────────────────────────────────────────────

_MAX_CROSS_CHARS = 1500  # cap each agent's reply before cross-injecting

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


def _build_cross_context(sid: str, my_name: str, participants: dict) -> str:
    """Return the last SUCCESSFUL reply from every other participant."""
    parts = []
    for name in participants:
        if name == my_name:
            continue
        history = load_session(sid, name)
        last = next(
            (m.content for m in reversed(history)
             if m.role == "assistant" and not _is_relay_failure(m.content)),
            None,
        )
        if last:
            snippet = last[:_MAX_CROSS_CHARS]
            if len(last) > _MAX_CROSS_CHARS:
                snippet += "\n[…truncated — see full reply in browser]"
            parts.append(f"[{name.upper()}]: {snippet}")
    if not parts:
        return ""
    return "The other panelists said:\n\n" + "\n\n".join(parts)


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

    if is_first:
        # Use the enriched system prompt built at session creation (stored in the
        # session's system message) — NOT cfg.system_prompt, which is the raw yaml
        # field and lacks the group-conversation framing ("You are in a panel with…").
        sys_msg = next((m.content for m in history if m.role == "system"), cfg.system_prompt)
        # The browser tab may have prior conversation history from a different session.
        # The separator line signals a hard context break so the model doesn't answer
        # whatever was previously in the window.
        return (
            "=== NEW RELAY SESSION — START FRESH, IGNORE PRIOR MESSAGES IN THIS WINDOW ===\n\n"
            f"{sys_msg}\n\n---\n\n{user_msg}"
        )

    cross = _build_cross_context(sid, name, active)
    return f"{cross}\n\n---\n\nUser: {user_msg}" if cross else user_msg


def run_round(sid: str, participants: dict, user_msg: str, synthesize: bool = True) -> None:
    mgr = get_manager()

    def is_ready(name: str, cfg) -> bool:
        if getattr(cfg, "mode", "api") == "browser":
            return bool(mgr and mgr.has_agent(name))
        return agent_active(cfg)

    active = {n: c for n, c in participants.items() if is_ready(n, c)}
    if not active:
        st.warning("No agents are ready — link tabs in Agents or set API keys.")
        return

    browser_agents = {n: c for n, c in active.items()
                      if getattr(c, "mode", "api") == "browser"}
    api_agents     = {n: c for n, c in active.items()
                      if getattr(c, "mode", "api") != "browser"}

    # Build browser payloads BEFORE appending the user message to state.
    # is_first inside _build_browser_msg checks whether the agent has replied
    # before — that check is only valid on the pre-update history.
    payloads: dict = {}
    for name, cfg in browser_agents.items():
        payloads[name] = _build_browser_msg(sid, name, cfg, active, user_msg)

    # Now commit the user turn to all agent histories and the display log.
    append_display(sid, {"type": "user", "content": user_msg})
    for name in active:
        append_message(sid, name, Message("user", user_msg))

    replies: dict = {}

    # ── Browser agents — submit all at once, wait in parallel ─────────────────
    if browser_agents and payloads and mgr:
        agent_list = ", ".join(payloads.keys())
        with st.spinner(f"Waiting for {agent_list}…"):
            try:
                batch = mgr.send_batch(payloads)
                replies.update(batch)
            except Exception as e:
                st.error(f"Batch send failed: {e}")

        for name in browser_agents:
            if name not in replies:
                continue
            with st.chat_message(name, avatar=avatar(name)):
                st.markdown(f"**{name.upper()}**")
                st.markdown(replies[name])

    # ── API agents — sequential with streaming ─────────────────────────────────
    for name, cfg in api_agents.items():
        with st.chat_message(name, avatar=avatar(name)):
            st.markdown(f"**{name.upper()}**")
            try:
                reply = st.write_stream(stream_agent(cfg, load_session(sid, name)))
                replies[name] = reply
            except Exception as e:
                st.error(f"**{name.upper()} failed:** {e}")

    # ── Persist replies ────────────────────────────────────────────────────────
    for name, reply in replies.items():
        append_display(sid, {"type": "reply", "agent": name, "content": reply})
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
        run_synthesis(sid, user_msg, successful_replies)

    # ── GitHub (background) ───────────────────────────────────────────────────
    if replies:
        repo = get_github_repo()
        if repo:
            meta = load_meta(sid)
            commit_async(_gh_append_round, repo, meta.get("topic", "conversation"),
                         list(active.keys()), user_msg, replies)

# ── Session state ─────────────────────────────────────────────────────────────

if "active_session" not in st.session_state: st.session_state.active_session = None
if "edit_agent"     not in st.session_state: st.session_state.edit_agent     = None

# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

active = active_agents()

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
                    if st.button(label, key=f"s_{s.name}", use_container_width=True):
                        st.session_state.active_session = s.name; st.rerun()
                with c2:
                    if st.button("🗑", key=f"d_{s.name}"):
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
        topic        = st.text_input("Topic", placeholder="e.g. Is AGI near?")
        new_chat_btn = st.button("＋ Start", type="primary", use_container_width=True)

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
            synthesize = st.toggle("🔮 Synthesizer", value=True,
                                   help="After each round, a neutral pass summarizes agreements, tensions, and missed angles.")

            # ── File attachment ───────────────────────────────────────────────
            uploaded = st.file_uploader(
                "Attach a file (content included in next message)",
                type=["txt","md","py","js","ts","jsx","tsx","html","css","json",
                      "yaml","yml","toml","csv","xml","sh","sql","go","rs","java",
                      "c","cpp","h","rb","php","swift","kt","r","tex","pdf"],
                label_visibility="collapsed",
                key=f"upload_{sid}",
            )
            if uploaded:
                try:
                    if uploaded.type == "application/pdf":
                        import io
                        try:
                            import pypdf
                            reader = pypdf.PdfReader(io.BytesIO(uploaded.read()))
                            file_text = "\n".join(p.extract_text() or "" for p in reader.pages)
                        except ImportError:
                            file_text = "[PDF attached — install pypdf to extract text: pip install pypdf]"
                    else:
                        file_text = uploaded.read().decode("utf-8", errors="replace")
                    st.session_state[f"file_content_{sid}"] = (uploaded.name, file_text)
                    st.caption(f"📎 **{uploaded.name}** ({len(file_text):,} chars) — will be included in next message")
                except Exception as e:
                    st.warning(f"Could not read file: {e}")
            elif f"file_content_{sid}" in st.session_state:
                name, _ = st.session_state[f"file_content_{sid}"]
                col1, col2 = st.columns([6, 1])
                with col1:
                    st.caption(f"📎 **{name}** attached")
                with col2:
                    if st.button("✕", key="clear_file"):
                        del st.session_state[f"file_content_{sid}"]
                        st.rerun()

            # ── Chat input ────────────────────────────────────────────────────
            if user_input := st.chat_input("Message all agents…"):
                file_attachment = st.session_state.pop(f"file_content_{sid}", None)
                if file_attachment:
                    fname, ftext = file_attachment
                    full_msg = f"[File: {fname}]\n```\n{ftext}\n```\n\n{user_input}" if user_input.strip() else f"[File: {fname}]\n```\n{ftext}\n```"
                else:
                    full_msg = user_input
                with st.chat_message("user", avatar="🧑"):
                    if file_attachment:
                        st.caption(f"📎 {file_attachment[0]}")
                    st.markdown(user_input)
                run_round(sid, participants, full_msg, synthesize=synthesize)
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
        mgr = get_manager()
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

        st.divider()
        if not agents_cfg:
            st.info("No agents yet — click **＋ New Agent** in the sidebar.")

        # ── Per-agent rows ────────────────────────────────────────────────────
        for name, cfg in agents_cfg.items():
            is_browser = getattr(cfg, "mode", "api") == "browser"
            has_tab    = mgr and mgr.has_agent(name) if is_browser else False
            is_on      = agent_active(cfg)

            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([5, 2, 1, 1])
                with c1:
                    if is_browser:
                        site   = site_for_agent(name)
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
                            if st.button("Unassign", key=f"ua_{name}", use_container_width=True):
                                mgr.unassign(name); st.rerun()
                        else:
                            site = site_for_agent(name)
                            is_claude = site and site.get("key") == "claude"
                            bc1, bc2 = st.columns(2)
                            with bc1:
                                if st.button("Find Tab", key=f"ft_{name}", use_container_width=True,
                                             type="primary" if is_claude else "secondary"):
                                    found = find_tab_for_agent(mgr, name)
                                    if found:
                                        st.rerun()
                                    else:
                                        site_key = (site_for_agent(name) or {}).get("url_match", "the site")
                                        st.session_state[f"tab_err_{name}"] = f"No open {site_key} tab found — open it in Chrome first, then click Find Tab"
                                        st.rerun()
                            with bc2:
                                if st.button("Open Tab", key=f"ot_{name}", use_container_width=True):
                                    with st.spinner(f"Opening {name.upper()}…"):
                                        try:
                                            mgr.open_tab(name); st.rerun()
                                        except Exception as e:
                                            st.session_state[f"tab_err_{name}"] = str(e); st.rerun()
                            err = st.session_state.pop(f"tab_err_{name}", None)
                            if err:
                                st.error(err)
                            if is_claude and not has_tab:
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
            if PLAYWRIGHT_AVAILABLE and site_for_agent(new_name.strip()):
                st.caption(f"Will open: {site_for_agent(new_name.strip())['url']}")

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
                                                   new_prompt.strip(), new_mode)
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

        upd_prompt = st.text_area("System Prompt", value=cfg.system_prompt, height=260)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save", type="primary", use_container_width=True):
                agents_cfg[ea] = AgentConfig(ea, upd_provider, upd_model,
                                             upd_prompt.strip(), upd_mode)
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
