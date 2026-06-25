# PROJECT_NOTES.md

Working notes for whoever (human or agent) picks this codebase up next.
README.md is the user-facing overview; this file is the engineering
diary — audit results, decisions, gotchas, and the rules that keep the
browser-tab safety model from quietly eroding.

## What Relay does

Push one message, multiple AI agents respond through their own
already-logged-in browser tabs (no API key needed), each sees the others'
replies, and they go back and forth for N rounds (or until stopped), with
an optional synthesis pass. `app.py` (Streamlit UI + round orchestration)
+ `browser_relay.py` (Playwright/CDP transport) is the actual product.
`relay.py` doubles as a shared library for `app.py` and a separate
API-only CLI with its own canonical-transcript engine — see README.md's
"two systems" section before touching either.

## Run & Operate

- `streamlit run app.py`
- Chrome must already be running with `--remote-debugging-port=9222` and
  a persistent, already-authenticated profile (Settings → Chrome Setup
  has the exact commands).
- Open each agent's site in a tab yourself, then use **Find Tab** (or
  **Open Tab**) per agent in the Agents screen.
- `python test_relay.py` runs the offline suite (syntax + `relay.py`
  canonical-transcript logic + `browser_relay.py` checks). It does not
  exercise live browser/CDP behavior — there's no substitute for manually
  verifying a real tab assignment after touching `browser_relay.py` or
  `app.py`'s tab-finding code.

## Where things live

| File | Owns |
|---|---|
| `app.py` | Round orchestration, payload construction, cross-injection, synthesis, quarantine, control panel, Agents/Settings screens, **Find Tab** UI and the scanned-tabs debug table, speed-diagnostics rendering. |
| `browser_relay.py` | `SITES` selectors, `BrowserManager` (single CDP worker thread), `site_for_agent`/`classify_tab_url`, submit verification, response extraction, per-stage timing instrumentation. |
| `relay.py` | `AgentConfig`/`Message`/`load_agents`/`load_session` shared library, plus the standalone CLI's canonical-transcript engine. |
| `relay_log.py` | Shared rotating logger. |
| `github_relay.py` | Optional async archival to GitHub — background thread only, never in the hot path. |
| `agents.yaml` | Agent roster: provider, model, mode, system prompt, optional `site_key` override. |
| `test_relay.py` | Offline test suite. |

## Architecture decisions

- **One dedicated Playwright worker thread** (`BrowserManager._run`)
  processes a command queue. Playwright's sync API is not thread-safe
  across a single CDP connection — this is why everything is serialized
  on one thread, sequentially, even in `send_batch`. Do not "fix" this
  with a thread pool; it would break the one CDP connection model, not
  speed it up.
- **`site_key` override beats name inference, always.** `site_for_agent`
  infers a site from an agent's name only when no explicit `site_key` is
  set, and that inference always maps any "claude"-containing name to
  `claude_chat` — never `claude_code`. This is deliberate: agents like
  `claude_code_architect`/`claude_code_pragmatist` in `agents.yaml` are
  ordinary chat personas with code-flavored *names*; they are not drivers
  for the literal `claude.ai/code` product. The only way an agent is ever
  treated as a Claude Code driver is an explicit `site_key: claude_code`
  in its config — never inferred from naming. Do not loosen this.
- **GitHub is archive, not mediator.** `github_relay.py` writes happen on
  a background thread (`commit_async`) and are never read back into a
  live round's prompt construction. Keeping GitHub out of the hot path
  was an explicit design constraint, not an oversight — see "GitHub
  pointer experiment" below before changing this.

## Gotchas

- A failed round (tab not found, send never verified) must not count as
  "this agent has replied" — `_build_browser_msg`'s `is_first` check
  explicitly excludes failure-shaped content, otherwise a round-1 failure
  would permanently skip sending the system prompt on every later retry.
- Stop-button-hidden and new-message-DOM-node are not atomic signals on
  most sites — `_make_envelope` gives element counts a short grace window
  before deciding a capture is stale. This is a checkpoint, not a
  guarantee; don't remove the grace window to "simplify" the polling loop.
- `window.name = "RELAY_AGENT:<agent>"` is the tab-identity marker. An
  unmarked tab is treated as available (not rejected) so pre-existing
  sessions don't break — only a marker that is *present and wrong* is a
  hard mismatch. Don't change this to "unmarked = reject" without
  understanding it breaks every tab assigned before the marker existed.
- `env_meta` in `app.py` (`{k: v for k, v in raw_envs.get(name, {}).items() if k != "payload"}`)
  means any new key added to an envelope dict (`timing`, `site_key`,
  `diagnostics`, etc.) automatically flows into `display.jsonl` and the
  debug UI with zero new storage-layer code. When adding a new envelope
  field, you almost never need to touch the persistence layer — just set
  the key on the dict.

## Browser loop trust ladder

From least to most trusted, what Relay actually verifies before treating
a captured string as an agent's real reply:

1. Tab is still marked for the agent being addressed (`window.name`
   check) — catches drift between same-provider personas sharing a
   domain.
2. The prompt that was typed is verified actually present in the composer
   before send is clicked (`_verify_prompt_inserted`).
3. Submission is verified (stop-button lifecycle / send-button state),
   with `fast_mode` patience tiers in batch sends so one stuck agent can't
   block every other agent in the same sequential loop from even starting
   to type.
4. Response capture prefers a fresh semantic container; falls back to
   element-count-incremented, then to a stable-text-completion heuristic;
   each tier produces a different `capture_method` / reply state (next
   section) rather than being silently treated as equally trustworthy.
5. Quality validation (echo detection, emptiness, identical-to-prior-reply)
   runs even after capture succeeds — a successfully captured string can
   still be quarantined.

## Reply states

Every captured reply lands in exactly one of four states (shown as `📊`
per-round stats, and per-agent in 🔍 Debug/Recovery):

- **confirmed** — fresh semantic container detected, or an unambiguous
  page-navigation signal.
- **semantic_unconfirmed** — element count never incremented even after
  grace-window retries, but the text passes strict echo/duplicate checks.
  Treated as real and cross-injected, but flagged lower-confidence.
  Meant to be rare — repeated occurrences on one provider are a
  selector/timing bug to fix, not something to rely on indefinitely.
- **quarantined** — captured text was stale, an echo, identical to a
  prior reply, empty, or otherwise unsafe to cross-inject. Shown as a
  system notice, never cross-injected as real speech.
- **transport_failure** — prompt insertion, send verification, tab
  identity, or response capture failed outright before any content
  judgment was possible.

## Failure-stage taxonomy

Diagnostics (`raw["diagnostics"]["failure_stage"]`, also `failure_stage`
on error envelopes) classify *where* a failure happened, distinct from
*which reply state* it produced:

- `construction` — building the prompt string itself failed (not
  currently expected to fail, but the slot exists in the schema).
- `assignment` — no tab found / tab-identity mismatch before any send
  was attempted.
- `insertion` — typing/pasting into the composer raised before insertion
  could be verified.
- `send` / `transport` — insertion succeeded but send verification or the
  wait for a response failed outright.
- `capture` — a response was detected but extracting usable text failed.
- `validation` — capture succeeded but quality/strict-check validation
  rejected it (quarantined).
- `cross_injection` — reserved for failures specific to bundling other
  agents' replies into context; not separately distinguished from
  `validation` today.

## Claude chat vs Claude Code tab rules

`claude.ai/code` (Claude Code's web IDE view) and a normal `claude.ai`
chat tab are different products with different DOM surfaces, and Relay
must never confuse them:

- `classify_tab_url(url)` labels every scanned tab as `claude_code`,
  `claude_chat`, `claude_other` (claude.ai but `/settings`, `/account`,
  `/projects`, `/team` — not a chat composer), one of the other `SITES`
  keys, or `unsupported`.
- A normal chat persona (`site_key` empty or `claude_chat`, inferred from
  any "claude"-containing agent name) only ever matches `claude_chat` tabs.
  **Find Tab explicitly rejects `claude.ai/code`** for these agents with:
  *"Found claude.ai/code, but this agent is configured as a Claude chat
  persona, so the tab was rejected."*
- A dedicated Claude Code agent (`site_key: claude_code`, never inferred
  from name) only ever matches `claude_code`-classified tabs. If no such
  tab is open: *"No Claude Code tab found. Open claude.ai/code, or
  configure this agent as a normal Claude chat persona."*
- `claude_code`'s `SITES` entry is marked `experimental: True`. If its
  composer/send/response selectors don't resolve, fast-health reports it
  as `unsupported — claude.ai/code has no known composer/send/response
  selectors yet` rather than failing silently or pretending it works.
- The Agents screen's scanned-tabs debug table
  (`index | title | url | classification | assigned_to | reason`) is the
  fastest way to see why Find Tab did or didn't match a given tab.

## Speed diagnostics

Each agent-round produces a `diagnostics` dict (rides through
`display.jsonl` via the existing `env_meta` mechanism, no new storage
code) with: `round_id`, `agent`, `prompt_chars`, `files_included`,
`tab_classification`, `construction_ms`, `insertion_ms`, `send_verify_ms`,
`wait_response_ms`, `capture_ms`, `capture_method`, `reply_state`,
`failure_stage`, `failure_reason`. Rendered as a compact table in 🔍
Debug/Recovery so a slow round can be attributed to prompt construction
vs composer insertion vs send verification vs waiting on the model vs
capture/selector issues, instead of one undifferentiated "it was slow."

`capture_ms` is currently always `None` — the existing polling loop
interleaves "wait for a new response" and "extract its text" rather than
timing them as two separate phases. Left honestly blank rather than
fabricated; splitting it out would mean restructuring `_wait_and_read`'s
polling loop, which this pass deliberately did not do.

**Concise mode** (✂️ toggle in the control panel, off by default) appends
"Answer in 2–4 short paragraphs. Be direct." to the outgoing prompt. It
does not change cross-context truncation caps (`_MAX_CROSS_CHARS`,
`_CROSS_CONTEXT_THRESHOLD`, `_CROSS_CONTEXT_TIGHT_CAP` in
`_build_cross_context`, which already only send the latest valid reply
per agent, not the full transcript) — those were already doing most of
the payload-size work before this toggle existed. Prompt char count is
logged per agent per round (`relay.log`) and shown in the diagnostics
table regardless of whether concise mode is on, so a size improvement is
directly observable, not just assumed.

## File mode (experimental, off by default)

The 📎 **File mode** control-panel toggle addresses large pasted text
occasionally tripping up a composer (slow re-render, paste timing out,
what users see as a "batch error") on rounds where the outgoing message —
system prompt, cross-context, or both — is still large even after the
existing `_MAX_CROSS_CHARS`/`_CROSS_CONTEXT_TIGHT_CAP` truncation:

- `_prepare_attachment()` (app.py) writes the full outgoing message to
  `context_files/context_{sid}_{agent}_r{round}_{stamp}.txt` whenever File
  mode is on **and** the message is at least `_FILE_ATTACH_THRESHOLD_CHARS`
  (3000) chars, returning `{"path", "pointer"}` — `pointer` is a short
  "see attached file" message used for typing instead of the full text.
- `BrowserManager.send_batch()`/`send_message()` take an optional
  `attachments` arg carrying that dict per agent. The worker thread tries
  `_attach_file()` (browser_relay.py) before typing; `_attach_file` uses
  each `SITES` entry's new `file_input` selector (`input[type="file"]` —
  a generic, **UNVERIFIED** guess, not confirmed against any live tab, same
  caveat as `claude_code`'s composer selectors).
- **Fallback is mandatory, not optional**: if `_attach_file` returns False
  for any reason, the worker types the original full message exactly as
  it would with File mode off. File mode can only ever skip a large paste
  on success — it can never cause content to be silently dropped on
  failure. This mirrors the project's established pattern for any
  unverified-selector capability (see `claude_code`'s `experimental: True`
  entry above) — default off, never trusted blindly, always a safe fallback.
- Diagnostics: each agent's `diagnostics` dict gets `context_mode`
  (`"inline"` / `"file"` / `"file_failed_fallback_inline"`) and
  `files_included` (the attachment path, if one was written this round),
  both rendered in the 🔍 Debug/Recovery speed-diagnostics table.
- **Not verified against a live tab** in this sandbox (no real Chrome/CDP
  available here) — the `file_input` selector and the resulting attach
  behavior on each site need confirming with a real tab before relying on
  File mode in production. Until then, expect `context_mode` to read
  `file_failed_fallback_inline` on most/all providers, which is the safe
  (not silently broken) failure mode this was designed for.

## GitHub is archive/pointer experiment, not default mediator

`github_relay.py` commits one markdown file per topic to a repo,
in a background thread, purely as a shareable, human/agent-readable
archive of a conversation. It is intentionally **not** wired into prompt
construction — no round reads back from GitHub to build context, and
turning that on by default was explicitly out of scope for this pass.

A possible future experiment: instead of (or in addition to) full
cross-context text, send agents a *pointer* — the raw GitHub URL for the
topic file — and let an agent with repo/web access pull full context
itself on demand. This was **not implemented**: it would need to be
isolated behind a flag that defaults to off, and no version of it turned
out to be trivial enough to justify adding in this pass without risking
the "GitHub becomes part of the hot path" outcome this project has
deliberately avoided so far. Revisit only if a concrete need shows up,
and keep it default-off when it does.

## Requirement-1 audit (this pass)

Verdicts from inspecting `browser_relay.py`, `app.py`, `agents.yaml`,
and README.md before making any changes:

| Item | Verdict | Notes |
|---|---|---|
| One-agent-one-tab duplicate rejection | PRESENT | `window.name` marker checked on assignment. |
| Pre-run duplicate validation | PRESENT | `find_tab_for_agent`/`auto_assign_tabs` track a `used` set / marker checks. |
| Pre-send tab ownership validation | PRESENT | Identity re-checked before every send, not just at assignment time. |
| Prompt insertion verification | PRESENT | `_verify_prompt_inserted`, called from `_type_and_submit`. |
| Send verification | PRESENT | Stop-button lifecycle / send-button state checks with tiered patience. |
| Reply states (confirmed/semantic_unconfirmed/quarantined/transport_failure) | PRESENT | `transport_stats` tally in `run_round`; documented in README. |
| Per-round transport stats | PRESENT | `transport_stats` dict, rendered as `📊` caption per round. |
| `send_message()` same envelope shape as `send_batch()` | PRESENT | Both go through `_make_envelope`/`_error_envelope`; README explicitly calls out this used to be a separate, unhardened path and was fixed. |
| `PROJECT_NOTES.md` | ABSENT (until this pass) | Did not exist; created as part of this work. |
| Failure-stage logging (construction/assignment/insertion/send/capture/validation/cross_injection) | PARTIAL → PRESENT (this pass) | Stages existed implicitly in code structure/log messages but were not a named, queryable field on envelopes before this pass; `failure_stage` is now stamped on error envelopes and the new `diagnostics` dict. |

## Verification limitations (this pass)

This sandbox has no live Chrome instance or `claude.ai/code` tab, so the
user's 11-step manual verification checklist (open a real Claude Code
tab, confirm classification, confirm rejection by chat personas, run a
live 2-agent round, inspect real Debug/Recovery timings) could not be
executed end-to-end here. What *was* verified: `ast.parse` syntax checks
on every edited file, and manual code-path tracing of
`classify_tab_url`/`site_for_agent`/`find_tab_for_agent`/`auto_assign_tabs`
against the required example messages and classification rules. A human
with a real Chrome + CDP setup should run the 11-step checklist before
trusting this in production.
