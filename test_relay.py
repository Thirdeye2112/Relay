#!/usr/bin/env python3
"""Offline test suite for relay.py and browser_relay.py logic."""
import sys, os, json, tempfile, importlib.util as ilu
from pathlib import Path

PASS = 0
FAIL = 0

def ok(label):
    global PASS
    PASS += 1
    print(f"  [OK]  {label}")

def fail(label, detail=""):
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {label}" + (f": {detail}" if detail else ""))

# ── Load modules ───────────────────────────────────────────────────────────────
print("\n--- Import checks ---")
import ast
for fname in ["relay.py", "browser_relay.py", "app.py"]:
    try:
        ast.parse(open(fname, encoding="utf-8").read())
        ok(f"{fname} syntax")
    except SyntaxError as e:
        fail(f"{fname} syntax", str(e))

spec = ilu.spec_from_file_location("relay", "relay.py")
relay = ilu.module_from_spec(spec)
spec.loader.exec_module(relay)

spec2 = ilu.spec_from_file_location("browser_relay", "browser_relay.py")
br = ilu.module_from_spec(spec2)
spec2.loader.exec_module(br)

# ── 1. Session round-trip ─────────────────────────────────────────────────────
print("\n--- Session round-trip ---")
with tempfile.TemporaryDirectory() as td:
    relay.SESSIONS_DIR = Path(td)
    sid, agent = "t1", "alice"
    msgs = [
        relay.Message("system",    "You are Alice."),
        relay.Message("user",      "Hello Alice."),
        relay.Message("assistant", "Hi there! How can I help?"),
    ]
    for m in msgs:
        relay.append_message(sid, agent, m)
    loaded = relay.load_session(sid, agent)
    try:
        assert len(loaded) == 3
        assert loaded[0].role == "system"
        assert loaded[1].role == "user"
        assert loaded[2].role == "assistant"
        assert loaded[2].content == "Hi there! How can I help?"
        ok("write/read round-trip (3 messages)")
    except AssertionError as e:
        fail("write/read round-trip", str(e))

    txt = relay.session_path(sid, agent).read_text(encoding="utf-8")
    if "~~~ RELAY:SYSTEM ~~~" in txt and "~~~ RELAY:USER ~~~" in txt:
        ok("file uses safe delimiters")
    else:
        fail("file uses safe delimiters", "delimiter not found in file")

# ── 2. Delimiter collision resistance ─────────────────────────────────────────
print("\n--- Delimiter collision ---")
with tempfile.TemporaryDirectory() as td:
    relay.SESSIONS_DIR = Path(td)
    sid, agent = "t2", "bob"
    tricky = "Normal text.\n[user]\nThat line looks like a role marker!\n[assistant]\nAnother fake."
    relay.append_message(sid, agent, relay.Message("user", tricky))
    relay.append_message(sid, agent, relay.Message("assistant", "Got it."))
    loaded = relay.load_session(sid, agent)
    try:
        assert len(loaded) == 2, f"got {len(loaded)} messages"
        assert loaded[0].content == tricky
        ok("[role] in LLM output does NOT split the transcript")
    except AssertionError as e:
        fail("delimiter collision resistance", str(e))

# ── 3. JSONL backward compat ──────────────────────────────────────────────────
print("\n--- JSONL backward compat ---")
with tempfile.TemporaryDirectory() as td:
    relay.SESSIONS_DIR = Path(td)
    sid, agent = "t3", "charlie"
    old_path = relay._legacy_path(sid, agent)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    with open(old_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "legacy message"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "legacy reply"}) + "\n")
    loaded = relay.load_session(sid, agent)
    try:
        assert len(loaded) == 2
        assert loaded[0].content == "legacy message"
        ok("old .jsonl files still load correctly")
    except AssertionError as e:
        fail("JSONL backward compat", str(e))

# ── 4. Context trimming ───────────────────────────────────────────────────────
print("\n--- Context trimming ---")
msgs50 = [relay.Message("system", "sys")] + [
    relay.Message("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    for i in range(50)
]
trimmed = relay.trim_context(msgs50)
sys_msgs = [m for m in trimmed if m.role == "system"]
rest     = [m for m in trimmed if m.role != "system"]
try:
    assert len(sys_msgs) == 1
    assert rest[0].content == "msg-0", "opening message must be pinned"
    assert len(rest) == relay.MAX_TURNS
    ok(f"50 msgs -> 1 sys + {relay.MAX_TURNS} turns, opening pinned")
except AssertionError as e:
    fail("context trimming", str(e))

# No-op when under limit
msgs5 = msgs50[:6]
trimmed5 = relay.trim_context(msgs5)
try:
    assert trimmed5 == msgs5
    ok("no trimming when under MAX_TURNS")
except AssertionError as e:
    fail("no-op trim", str(e))

# ── 5. site_for_agent matching ────────────────────────────────────────────────
print("\n--- site_for_agent ---")
cases = [
    # "claude"-containing names always infer claude_chat, never claude_code --
    # claude_code is only reachable via an explicit site_key override, even
    # for chat personas with code-flavored names like these two.
    ("claude_optimist",      "claude_chat"),
    ("claude_skeptic",       "claude_chat"),
    ("claude_code_architect","claude_chat"),
    ("claude_code_pragmatist","claude_chat"),
    ("chatgpt",              "chatgpt"),
    ("gemini",               "gemini"),
    ("perplexity",           "perplexity"),
    ("grok_agent",           "grok"),
    ("copilot",              "copilot"),
]
for name, expected in cases:
    site = br.site_for_agent(name)
    if site and site["key"] == expected:
        ok(f"{name!r} -> {expected}")
    else:
        got = site["key"] if site else None
        fail(f"{name!r} -> {expected}", f"got {got!r}")

if br.site_for_agent("random_unknown_xyz") is None:
    ok("unknown agent -> None")
else:
    fail("unknown agent", "should return None")

# An explicit override always wins, regardless of name inference.
site = br.site_for_agent("claude_optimist", "claude_code")
if site and site["key"] == "claude_code":
    ok("explicit site_key override -> claude_code")
else:
    fail("explicit site_key override", f"got {site['key'] if site else None!r}")

# ── 5b. classify_tab_url ───────────────────────────────────────────────────────
print("\n--- classify_tab_url ---")
url_cases = [
    ("https://claude.ai/code",          "claude_code"),
    ("https://claude.ai/chat/abc123",   "claude_chat"),
    ("https://claude.ai/new",           "claude_chat"),
    ("https://claude.ai/settings/profile", "claude_other"),
    ("https://chatgpt.com/c/abc",       "chatgpt"),
    ("https://example.com/whatever",    "unsupported"),
]
for url, expected in url_cases:
    got = br.classify_tab_url(url)
    if got == expected:
        ok(f"{url!r} -> {expected}")
    else:
        fail(f"{url!r} -> {expected}", f"got {got!r}")

# ── 6. _clean_diff ────────────────────────────────────────────────────────────
print("\n--- _clean_diff ---")
raw = (
    "Here is my actual answer about AI.\n\n"
    "Claude is AI and can make mistakes. Please double-check responses.\n"
    "Sonnet 4.6 Low\n\n"
    "Share Copy\n"
)
cleaned = br._clean_diff(raw)
try:
    assert "Here is my actual answer about AI." in cleaned
    assert "Claude is AI" not in cleaned
    assert "Sonnet" not in cleaned
    ok("UI chrome stripped, content preserved")
except AssertionError as e:
    fail("_clean_diff strips chrome", str(e))

try:
    assert br._clean_diff("OK") == "OK"
    ok("short clean response returned unchanged")
except AssertionError as e:
    fail("_clean_diff short response", str(e))

# ── 7. Round-1 ordering trace ─────────────────────────────────────────────────
print("\n--- Round-1 ordering ---")
with tempfile.TemporaryDirectory() as td:
    relay.SESSIONS_DIR = Path(td)
    sid = "r1-trace"
    opening = 'Topic: test\n\nGive your opening position.'

    # Simulate cmd_debate seeding (both agents get opening prompt at setup)
    relay.append_message(sid, "alice", relay.Message("system", "sys-a"))
    relay.append_message(sid, "bob",   relay.Message("system", "sys-b"))
    relay.append_message(sid, "alice", relay.Message("user", opening))
    relay.append_message(sid, "bob",   relay.Message("user", opening))

    # Simulate A's first reply + cross-inject into B
    relay.append_message(sid, "alice", relay.Message("assistant", "Alice reply"))
    relay.append_message(sid, "bob",   relay.Message("user", "[ALICE]: Alice reply"))

    bob = relay.load_session(sid, "bob")
    try:
        assert bob[1].content == opening,            "opening prompt is B's first user msg"
        assert bob[2].content == "[ALICE]: Alice reply", "cross-inject is B's second user msg"
        ok("B sees: system -> user(opening) -> user(A's reply) [correct order]")
    except AssertionError as e:
        fail("round-1 ordering", str(e))

    # first_round parameter removed from _run_rounds signature
    import inspect
    sig = inspect.signature(relay._run_rounds)
    if "first_round" not in sig.parameters:
        ok("first_round parameter removed from _run_rounds")
    else:
        fail("first_round parameter", "still present in signature")

# ── 8. provider: browser gives helpful error ──────────────────────────────────
print("\n--- provider: browser error ---")
fake_browser_agent = relay.AgentConfig(
    name="perplexity", provider="browser", model="", system_prompt="", mode="browser", base_url=""
)
try:
    relay.call_agent(fake_browser_agent, [])
    fail("provider: browser", "should have exited")
except SystemExit as e:
    if "Streamlit" in str(e):
        ok("provider: browser gives helpful Streamlit pointer")
    else:
        fail("provider: browser error message", f"got: {e}")

# ── 9. Broadcast inject (all) ─────────────────────────────────────────────────
print("\n--- Broadcast inject ---")
with tempfile.TemporaryDirectory() as td:
    relay.SESSIONS_DIR = Path(td)
    sid = "inject-test"
    relay.save_meta(sid, {"session_id": sid, "type": "debate", "topic": "t",
                           "agents": ["alice", "bob"], "rounds_completed": 0,
                           "created": "2026-01-01"})
    relay.append_message(sid, "alice", relay.Message("user", "first"))
    relay.append_message(sid, "bob",   relay.Message("user", "first"))

    # Simulate inject all
    for target in ["alice", "bob"]:
        relay.append_message(sid, target, relay.Message("user", "[MODERATOR]: wrap up"))

    alice_msgs = relay.load_session(sid, "alice")
    bob_msgs   = relay.load_session(sid, "bob")
    try:
        assert any("[MODERATOR]" in m.content for m in alice_msgs)
        assert any("[MODERATOR]" in m.content for m in bob_msgs)
        ok("moderator message appears in both agents' contexts")
    except AssertionError as e:
        fail("broadcast inject", str(e))

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  {PASS} passed  |  {FAIL} failed")
if FAIL:
    sys.exit(1)
