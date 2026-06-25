# Relay

Push one message, multiple AI agents respond, each sees the others' replies,
and they go back and forth for N rounds (or unlimited, until stopped) — with
an optional synthesis pass at the end. Agents run through their own
already-logged-in browser tab (Claude, ChatGPT, Gemini, Perplexity, etc. — no
API key, no extra cost) or, optionally, a direct API key if you have one.

Run it: `streamlit run app.py`

## The two systems in this repo — read this first

There are **two separate ways to run a Relay conversation**, with two
separate storage formats, that happen to live in the same `sessions/`
directory. Confusing this is the single easiest way to get lost in this
codebase.

1. **The Streamlit app** (`app.py` + `browser_relay.py`) — the actual
   product. Browser-first: agents are real, already-logged-in tabs that
   Relay drives via Playwright/CDP. This is what you run day to day.
2. **The CLI** (`python relay.py debate|panel|chat|...`) — a separate,
   API-only tool with its own more sophisticated canonical-transcript engine
   (event sourcing, role-alternation enforced by construction). It does not
   use browser tabs at all, and the Streamlit app does not use it. It exists
   for quick, scriptable, API-key-only debates/panels outside the UI.

A given session is **either** a CLI session (has a `transcript.jsonl`) **or**
a Streamlit-app session (has `display.jsonl` + per-agent legacy `.txt`
files) — `relay.py`'s `load_session()` checks which one exists and reads
accordingly. They were never meant to be mixed within one session.

## File map

| File | Owns |
|---|---|
| `app.py` | The Streamlit UI and all round orchestration: building each round's payload, sending it, cross-injecting replies, synthesis, reply validation/quarantine, the control panel (agents/rounds/stop/retry), session + agent management screens. **Start here** to understand the actual conversation loop. |
| `browser_relay.py` | The browser transport layer. `SITES` (per-provider CSS selectors — claude/chatgpt/gemini/perplexity/grok/copilot), `BrowserManager` (the CDP connection, one dedicated thread, all Playwright calls), submit verification, response extraction, health checks. **Start here** for anything about a specific AI site breaking, selectors drifting, or send/read reliability. |
| `relay.py` | Two roles in one file: (a) a shared library `app.py` imports (`AgentConfig`, `Message`, `load_agents`, `load_session`, `save_meta`/`load_meta`), and (b) the standalone CLI tool with its own canonical-transcript engine (`append_event`/`project()`/`_enforce_alternation`). Read the module docstring at the top for the CLI command list. |
| `relay_log.py` | Shared rotating file logger (`relay.log`, 5MB × 3 backups) used by `app.py`/`browser_relay.py`. Falls back to stdlib `logging` if unavailable — never a hard dependency. |
| `github_relay.py` | Optional: commits each round to a GitHub repo as one markdown file per topic, so any agent with repo access can read the raw URL for full context. Runs in a background thread, never blocks the UI. |
| `test_relay.py` | Offline test suite — syntax checks plus logic tests for `relay.py`'s canonical-transcript engine and `browser_relay.py`. Run with `python test_relay.py`. |
| `agents.yaml` | The agent roster: name, provider, model, mode (`api` or `browser`), system prompt. Loaded by `load_agents()`. |

## The Streamlit app's round loop, concretely

1. `run_round(sid, participants, user_msg)` is the unit of work — one human
   message in, one reply from every active agent out. `run_auto_rounds()`
   just calls it repeatedly (N times, or unlimited until Stop) with an
   auto-generated "continue the discussion" prompt for rounds after the
   first.
2. For each browser agent, `_build_browser_msg()` decides what to send: the
   full system prompt on an agent's first-ever reply, or just the other
   agents' latest replies (`_build_cross_context()`) plus the new message on
   later rounds.
3. `browser_relay.BrowserManager.send_batch()` submits to every tab
   (sequentially, fast) then polls all of them in parallel for replies —
   total wait is ≈ the slowest agent, not the sum.
4. Every reply comes back wrapped in a lineage envelope (`validated`,
   `capture_method`, element-count bookkeeping) — `run_round` quarantines
   anything that looks like a stale DOM read or a prompt echo before it's
   ever treated as a real reply. Sentinel failures (`[Error: ...]`,
   `[Timed out]`, etc. — see `_is_relay_failure`) are shown to the user as a
   visible system notice, never cross-injected to other agents as if they
   were real speech.
5. Only successful replies get cross-injected and fed to the synthesizer.
   Synthesis prefers reusing one of the round's already-replying browser
   agents (no extra API key/cost); falls back to a direct Anthropic API call
   only for all-API-mode sessions with a funded key.
6. `meta.json`'s `round_count` and a `round_id` stamped on every
   `display.jsonl` event are this app's own bookkeeping — separate from, and
   not shared with, the CLI's canonical transcript.

## Setup

- Chrome must be running with `--remote-debugging-port=9222` and a
  persistent profile (see **Settings → Chrome Setup** in the app for the
  exact PowerShell commands and the pre-auth-then-relaunch flow — Google's
  OAuth blocks sign-in inside an already-debug-mode browser, so auth happens
  in a clean window first).
- API-mode agents need the matching key in **Settings** (`ANTHROPIC_API_KEY`
  / `OPENAI_API_KEY`), saved to `keys.json` (gitignored, local only).
- GitHub export is optional — needs a personal access token in Settings.

## Known limitations, stated plainly

- The "Stop After Round" control checks a flag between loop iterations
  within one continuous Python call. Whether a click mid-run is guaranteed
  to land cleanly between rounds (rather than mid-round) depends on
  Streamlit's script-interruption behavior, which hasn't been proven safe —
  see the docstring on `run_auto_rounds` in `app.py`.
- The page-text-diff fallback path (used only when an AI site's CSS
  selectors are stale) can occasionally return truncated text that isn't
  one of the known failure sentinels — a known, accepted gap, not silently
  ignored.
