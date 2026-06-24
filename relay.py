#!/usr/bin/env python3
"""
relay -- minimal AI agent debate & collaboration shell

Commands:
  debate <agent-a> <agent-b> "<topic>" [--rounds N]   start a new debate
  resume <session-id>               [--rounds N]        continue a debate
  chat   <agent>                    [--session ID]      talk to a single agent
  inject <session-id> <agent|all> "<message>"           inject mid-debate
  list                                                   show all sessions
  show   <session-id>                                   print full transcript
"""

import os
import re
import sys
import json
import argparse
import datetime
from pathlib import Path
from dataclasses import dataclass

try:
    import yaml
except ImportError:
    sys.exit("pyyaml not installed -- run: pip install -r requirements.txt")

# -- Paths ---------------------------------------------------------------------

BASE_DIR     = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
AGENTS_FILE  = BASE_DIR / "agents.yaml"

# -- Types ---------------------------------------------------------------------

@dataclass
class AgentConfig:
    name:          str
    provider:      str   # "anthropic" | "openai" | "perplexity" | "browser"
    model:         str
    system_prompt: str
    mode:          str = "api"   # "api" | "browser"
    base_url:      str = ""

@dataclass
class Message:
    role:    str   # "system" | "user" | "assistant"
    content: str

# -- Agent registry ------------------------------------------------------------

def load_agents() -> dict[str, AgentConfig]:
    if not AGENTS_FILE.exists():
        sys.exit(f"agents.yaml not found at {AGENTS_FILE}")
    with open(AGENTS_FILE, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    agents = {}
    for name, cfg in raw.get("agents", {}).items():
        provider = cfg.get("provider", "anthropic").lower()
        base_url = cfg.get("base_url", "")
        if provider == "perplexity" and not base_url:
            base_url = "https://api.perplexity.ai"
        agents[name] = AgentConfig(
            name          = name,
            provider      = provider,
            model         = cfg.get("model", "claude-sonnet-4-6"),
            system_prompt = cfg.get("system_prompt", ""),
            mode          = cfg.get("mode", "api"),
            base_url      = base_url,
        )
    return agents

# -- Session I/O (plain-text format with JSONL backward compat) ----------------

# Unique delimiter unlikely to appear in LLM output.
# Format:  \n~~~ RELAY:ROLE ~~~\ncontent\n
# The regex matches the marker ONLY when it is the entire line (^ and $ with MULTILINE),
# so `[user]` embedded in prose does NOT accidentally split.
_MARKER_RE = re.compile(r"^~~~ RELAY:(SYSTEM|USER|ASSISTANT) ~~~$", re.MULTILINE)
_MARKER    = "~~~ RELAY:{} ~~~"

def session_path(session_id: str, agent_name: str) -> Path:
    return SESSIONS_DIR / session_id / f"{agent_name}.txt"

def _legacy_path(session_id: str, agent_name: str) -> Path:
    return SESSIONS_DIR / session_id / f"{agent_name}.jsonl"

def meta_path(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "meta.json"

def load_session(session_id: str, agent_name: str) -> list[Message]:
    path   = session_path(session_id, agent_name)
    legacy = _legacy_path(session_id, agent_name)
    if path.exists():
        return _parse_text(path.read_text(encoding="utf-8"))
    if legacy.exists():
        return _parse_jsonl(legacy.read_text(encoding="utf-8"))
    return []

def _parse_text(text: str) -> list[Message]:
    parts = _MARKER_RE.split(text)
    # parts: ['preamble', 'SYSTEM', 'content\n', 'USER', 'content\n', ...]
    messages = []
    for i in range(1, len(parts), 2):
        role    = parts[i].lower()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        messages.append(Message(role=role, content=content))
    return messages

def _parse_jsonl(text: str) -> list[Message]:
    messages = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            messages.append(Message(role=d["role"], content=d["content"]))
    return messages

def append_message(session_id: str, agent_name: str, msg: Message) -> None:
    path = session_path(session_id, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n{_MARKER.format(msg.role.upper())}\n{msg.content}\n")

def load_meta(session_id: str) -> dict:
    path = meta_path(session_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

def save_meta(session_id: str, meta: dict) -> None:
    path = meta_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

# -- Client cache (one TLS connection per provider) ----------------------------

_CLIENTS: dict = {}

def _get_openai_client(api_key: str, base_url: str | None):
    from openai import OpenAI
    key = (hash(api_key), base_url)
    if key not in _CLIENTS:
        kwargs: dict = {"api_key": api_key, "timeout": 60.0, "max_retries": 2}
        if base_url:
            kwargs["base_url"] = base_url
        _CLIENTS[key] = OpenAI(**kwargs)
    return _CLIENTS[key]

def _get_anthropic_client(api_key: str):
    import anthropic
    if "anthropic" not in _CLIENTS:
        _CLIENTS["anthropic"] = anthropic.Anthropic(
            api_key=api_key, timeout=60.0, max_retries=2
        )
    return _CLIENTS["anthropic"]

# -- API calls -----------------------------------------------------------------

def call_anthropic(model: str, messages: list[Message]) -> str:
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic SDK not installed -- run: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set")

    client = _get_anthropic_client(api_key)
    system = next((m.content for m in messages if m.role == "system"), "")
    api_msgs = [{"role": m.role, "content": m.content}
                for m in messages if m.role != "system"]

    response = client.messages.create(
        model=model, max_tokens=2048, system=system, messages=api_msgs,
    )
    return response.content[0].text

def call_openai_compatible(agent: AgentConfig, messages: list[Message]) -> str:
    try:
        from openai import OpenAI  # noqa: F401
    except ImportError:
        sys.exit("openai SDK not installed -- run: pip install openai")

    if agent.provider == "perplexity":
        api_key = os.environ.get("PERPLEXITY_API_KEY")
        if not api_key:
            sys.exit("PERPLEXITY_API_KEY not set")
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            sys.exit("OPENAI_API_KEY not set")

    client   = _get_openai_client(api_key, agent.base_url or None)
    response = client.chat.completions.create(
        model    = agent.model,
        messages = [{"role": m.role, "content": m.content} for m in messages],
        max_tokens = 2048,
    )
    return response.choices[0].message.content

def call_agent(agent: AgentConfig, messages: list[Message]) -> str:
    if agent.provider == "anthropic":
        return call_anthropic(agent.model, messages)
    if agent.provider in ("openai", "perplexity"):
        return call_openai_compatible(agent, messages)
    if agent.provider == "browser":
        sys.exit(
            f"Agent '{agent.name}' is configured as provider: browser.\n"
            "Browser agents run through the Streamlit app (streamlit run app.py), not this CLI.\n"
            "To use this CLI, set provider: anthropic / openai / perplexity in agents.yaml."
        )
    sys.exit(
        f"Unknown provider '{agent.provider}' -- must be anthropic, openai, or perplexity"
    )

MAX_TURNS = 30  # non-system messages kept per agent before trimming

def trim_context(messages: list[Message]) -> list[Message]:
    """Keep the system prompt + opening statement + last MAX_TURNS turns."""
    system = [m for m in messages if m.role == "system"][:1]
    rest   = [m for m in messages if m.role != "system"]
    if len(rest) <= MAX_TURNS:
        return system + rest
    # Pin the first message (opening prompt / task), then take the tail
    return system + [rest[0]] + rest[-(MAX_TURNS - 1):]


def safe_call_agent(agent: AgentConfig, messages: list[Message]) -> str:
    try:
        return call_agent(agent, messages)
    except Exception as e:
        return f"[ERROR from {agent.name} ({agent.provider}/{agent.model}): {type(e).__name__}: {e}]"

# -- System prompt -------------------------------------------------------------

def build_system(agent: AgentConfig, opponent: str, topic: str) -> str:
    header = (
        f"Your name is {agent.name.upper()}.\n"
        f"You are debating {opponent.upper()} on: \"{topic}\"\n\n"
        f"Address {opponent.upper()} by name when responding to their specific points.\n"
        f"End each response with: -- {agent.name.upper()}\n\n"
    )
    base = agent.system_prompt.replace("OPPONENT", opponent.upper())
    return header + base

# -- Print helpers -------------------------------------------------------------

DIVIDER = "=" * 62
SUBDIV  = "-" * 62

def banner(session_id: str, topic: str, a: str, b: str, rounds: int) -> None:
    print(f"\n{DIVIDER}")
    print(f"  SESSION  {session_id}")
    print(f"  TOPIC    {topic}")
    print(f"  AGENTS   {a.upper()} vs {b.upper()}")
    print(f"  ROUNDS   {rounds}")
    print(f"{DIVIDER}\n")

def print_reply(name: str, text: str) -> None:
    print(f"\n[{name.upper()}]\n{text}\n")

# -- debate command ------------------------------------------------------------

def cmd_debate(args, agents: dict[str, AgentConfig]) -> None:
    a_name, b_name = args.agent_a, args.agent_b
    topic  = args.topic
    rounds = args.rounds

    for n in (a_name, b_name):
        if n not in agents:
            sys.exit(f"Unknown agent: {n}  (check agents.yaml)")

    agent_a = agents[a_name]
    agent_b = agents[b_name]

    ts         = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id = f"debate-{a_name}-{b_name}-{ts}"

    meta = {
        "session_id":       session_id,
        "type":             "debate",
        "topic":            topic,
        "agents":           [a_name, b_name],
        "rounds_completed": 0,
        "created":          datetime.datetime.now().isoformat(),
    }
    save_meta(session_id, meta)

    append_message(session_id, a_name, Message("system", build_system(agent_a, b_name, topic)))
    append_message(session_id, b_name, Message("system", build_system(agent_b, a_name, topic)))

    # Both agents see the opening prompt before any cross-injection happens.
    # B must receive it here — not after A's reply — or B's history starts with
    # two consecutive user-role messages (A's injected reply then the topic),
    # which violates Anthropic's strict user/assistant alternation requirement.
    opening = f'Topic: "{topic}"\n\nGive your opening position.'
    append_message(session_id, a_name, Message("user", opening))
    append_message(session_id, b_name, Message("user", opening))

    banner(session_id, topic, a_name, b_name, rounds)
    _run_rounds(session_id, agent_a, agent_b, a_name, b_name, meta, rounds)

# -- resume command ------------------------------------------------------------

def cmd_resume(args, agents: dict[str, AgentConfig]) -> None:
    session_id = args.session_id
    meta       = load_meta(session_id)
    if not meta:
        sys.exit(f"Session not found: {session_id}")

    a_name, b_name = meta["agents"][0], meta["agents"][1]
    for n in (a_name, b_name):
        if n not in agents:
            sys.exit(f"Agent '{n}' missing from agents.yaml")

    agent_a = agents[a_name]
    agent_b = agents[b_name]
    rounds  = args.rounds

    print(f"\n{DIVIDER}")
    print(f"  RESUMING {session_id}")
    print(f"  TOPIC    {meta['topic']}")
    print(f"  AGENTS   {a_name.upper()} vs {b_name.upper()}")
    print(f"  DONE     {meta['rounds_completed']} rounds  +  {rounds} more")
    print(f"{DIVIDER}\n")

    _run_rounds(session_id, agent_a, agent_b, a_name, b_name, meta, rounds)

def _run_rounds(
    session_id: str,
    agent_a:    AgentConfig,
    agent_b:    AgentConfig,
    a_name:     str,
    b_name:     str,
    meta:       dict,
    rounds:     int,
) -> None:
    start = meta["rounds_completed"] + 1

    for r in range(start, start + rounds):
        print(f"\n{SUBDIV}")
        print(f"  Round {r}")
        print(f"{SUBDIV}")

        # Agent A speaks
        print(f"\n  [{a_name.upper()} thinking...]")
        reply_a = safe_call_agent(agent_a, trim_context(load_session(session_id, a_name)))
        append_message(session_id, a_name, Message("assistant", reply_a))
        # Cross-inject A's reply into B's context (B already has the opening prompt
        # from cmd_debate, so this lands in chronological order after it)
        append_message(session_id, b_name, Message("user", f"[{a_name.upper()}]: {reply_a}"))

        print_reply(a_name, reply_a)

        # Agent B speaks
        print(f"  [{b_name.upper()} thinking...]")
        reply_b = safe_call_agent(agent_b, trim_context(load_session(session_id, b_name)))
        append_message(session_id, b_name, Message("assistant", reply_b))
        # Cross-inject B's reply into A's context
        append_message(session_id, a_name, Message("user", f"[{b_name.upper()}]: {reply_b}"))

        print_reply(b_name, reply_b)

        meta["rounds_completed"] = r
        save_meta(session_id, meta)

    print(f"\n{DIVIDER}")
    print(f"  Done.  Resume:  python relay.py resume {session_id}")
    print(f"  Show:           python relay.py show   {session_id}")
    print(f"{DIVIDER}\n")

# -- chat command --------------------------------------------------------------

def cmd_chat(args, agents: dict[str, AgentConfig]) -> None:
    agent_name = args.agent
    if agent_name not in agents:
        sys.exit(f"Unknown agent: {agent_name}")

    agent = agents[agent_name]
    ts    = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    sid   = args.session or f"chat-{agent_name}-{ts}"

    if not load_session(sid, agent_name):
        # Use the raw system_prompt for chat (no opponent); still consistent with debate format
        sys_prompt = (
            f"Your name is {agent.name.upper()}.\n\n"
            f"End each response with: -- {agent.name.upper()}\n\n"
        ) + agent.system_prompt
        append_message(sid, agent_name, Message("system", sys_prompt))

    print(f"\n  Talking to {agent_name.upper()}  (session: {sid})")
    print(f"  Commands: /show  /exit\n")

    while True:
        try:
            user_input = input("[you] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input in ("/exit", "exit"):
            break
        if user_input == "/show":
            for m in load_session(sid, agent_name):
                if m.role != "system":
                    label = "you" if m.role == "user" else agent_name.upper()
                    print(f"\n[{label}]\n{m.content}")
            print()
            continue

        append_message(sid, agent_name, Message("user", user_input))
        print(f"\n  [{agent_name.upper()} thinking...]\n")
        reply = safe_call_agent(agent, trim_context(load_session(sid, agent_name)))
        append_message(sid, agent_name, Message("assistant", reply))
        print_reply(agent_name, reply)

# -- inject command ------------------------------------------------------------

def cmd_inject(args, agents: dict[str, AgentConfig]) -> None:
    session_id = args.session_id
    agent_arg  = args.agent      # agent name or "all"
    message    = args.message

    meta = load_meta(session_id)
    if not meta:
        sys.exit(f"Session not found: {session_id}")

    session_agents = meta.get("agents", [])
    if agent_arg == "all":
        targets = session_agents
    else:
        if agent_arg not in session_agents:
            sys.exit(f"Agent '{agent_arg}' is not in session {session_id}")
        targets = [agent_arg]

    for target in targets:
        append_message(session_id, target, Message("user", f"[MODERATOR]: {message}"))

    label = "all agents" if agent_arg == "all" else agent_arg.upper()
    print(f"\n  Injected into {label} in session {session_id}")
    print(f"  Run: python relay.py resume {session_id}\n")

# -- list command --------------------------------------------------------------

def cmd_list(args, agents: dict[str, AgentConfig]) -> None:
    if not SESSIONS_DIR.exists() or not list(SESSIONS_DIR.iterdir()):
        print("\n  No sessions yet.\n")
        return

    sessions = sorted(SESSIONS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    print(f"\n  {'SESSION':<44} {'AGENTS':<25} ROUNDS  TOPIC")
    print(f"  {'-'*95}")
    for s in sessions:
        m = load_meta(s.name)
        if not m:
            continue
        pair   = " vs ".join(a.upper() for a in m.get("agents", []))
        rounds = str(m.get("rounds_completed", "?"))
        topic  = m.get("topic", "")[:35]
        print(f"  {s.name:<44} {pair:<25} {rounds:<7} {topic}")
    print()

# -- show command --------------------------------------------------------------

def cmd_show(args, agents: dict[str, AgentConfig]) -> None:
    session_id = args.session_id
    meta       = load_meta(session_id)
    if not meta:
        sys.exit(f"Session not found: {session_id}")

    print(f"\n{DIVIDER}")
    print(f"  SESSION  {session_id}")
    print(f"  TOPIC    {meta.get('topic', '?')}")
    print(f"  ROUNDS   {meta.get('rounds_completed', '?')}")
    print(f"{DIVIDER}\n")

    for agent_name in meta.get("agents", []):
        print(f"\n{'-'*30} {agent_name.upper()} {'-'*30}\n")
        for msg in load_session(session_id, agent_name):
            if msg.role == "system":
                if getattr(args, "show_system", False):
                    print(f"[SYSTEM]\n{msg.content}\n")
                continue
            if msg.role == "assistant":
                print(f"[{agent_name.upper()}]\n{msg.content}\n")
            elif msg.role == "user":
                if msg.content.startswith("["):
                    print(f"{msg.content}\n")
                else:
                    print(f"[TOPIC]\n{msg.content}\n")

# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog        = "relay",
        description = "AI agent debate & collaboration shell",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
examples:
  python relay.py debate analyst critic "monorepo vs polyrepo" --rounds 5
  python relay.py resume debate-analyst-critic-20260623-143022 --rounds 3
  python relay.py inject debate-analyst-critic-20260623-143022 all "wrap up in one paragraph"
  python relay.py inject debate-analyst-critic-20260623-143022 analyst "what about CI cost?"
  python relay.py chat analyst
  python relay.py list
  python relay.py show debate-analyst-critic-20260623-143022
""",
    )
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("debate", help="Start a new debate")
    p.add_argument("agent_a")
    p.add_argument("agent_b")
    p.add_argument("topic")
    p.add_argument("--rounds", type=int, default=5)

    p = sub.add_parser("resume", help="Continue an existing debate")
    p.add_argument("session_id")
    p.add_argument("--rounds", type=int, default=3)

    p = sub.add_parser("chat", help="Talk to a single agent interactively")
    p.add_argument("agent")
    p.add_argument("--session", help="Resume a specific chat session ID")

    p = sub.add_parser("inject", help="Inject a message into a debate context")
    p.add_argument("session_id")
    p.add_argument("agent", help="Agent name, or 'all' to broadcast to every agent")
    p.add_argument("message")

    sub.add_parser("list", help="List all sessions")

    p = sub.add_parser("show", help="Print each agent's full context")
    p.add_argument("session_id")
    p.add_argument("--show-system", action="store_true",
                   help="Also print system prompts")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    dispatch = {
        "debate": cmd_debate,
        "resume": cmd_resume,
        "chat":   cmd_chat,
        "inject": cmd_inject,
        "list":   cmd_list,
        "show":   cmd_show,
    }
    dispatch[args.cmd](args, load_agents())

if __name__ == "__main__":
    main()
