# CLAUDE.md — Project Context & Continuation Guide

This file lets any AI agent (or human) pick this project up cold in a new
session. Read this first, then `README.md`, then `docs/architecture.md`.

## What this project is

A **take-home assignment for an AI startup job application** (owner: Sadiq).
The assignment email, verbatim requirements:

> **The Challenge: AI Customer Support Agent** — Build a fully functional web
> application for an AI Customer Support Agent that processes or denies
> e-commerce refunds using an LLM. It needs:
> 1. **Mock Data:** A CRM database (15 profiles) and a strict refund policy document.
> 2. **Agent Backend:** An agent loop (LangGraph, CrewAI, or raw function calling)
>    that dynamically calls tools to validate policy rules. Bonus: voice pipeline
>    (OpenAI Realtime API, ElevenLabs, or LiveKit).
> 3. **Frontend UI:** A clean interface with a customer chat interface and/or a
>    microphone voice component, alongside an admin dashboard showing real-time
>    agent reasoning logs.
>
> **Main deliverable:** a 7–10 minute Loom video showing (a) a live demo of a
> standard refund and an edge case / policy violation ("holding the line"),
> plus spoken interaction if voice was implemented; (b) a code tour of the
> repository architecture, tool orchestration, and voice handling; (c) where
> the agent handles failures/retries in the admin panel. Public GitHub repo +
> clean README required.

**Division of labor:** the AI assistant builds the code and docs; **Sadiq
records the Loom video and does the walkthrough himself** (see
`docs/demo-script.md` for the prepared script).

## Status (2026-08-06)

Everything below is BUILT, TESTED, and VERIFIED in the browser:

- [x] Mock CRM: 15 profiles in `backend/app/data/customers.json`, one per policy
      scenario; dates are `*_days_ago` offsets hydrated at load (demos never expire)
- [x] Strict policy doc: `backend/app/data/refund_policy.md` (rules R1–R12)
- [x] Deterministic policy engine (`services/policy.py`) implementing R1–R11 1:1
- [x] Agent loop, raw function calling (`agent/loop.py`): visible LLM retries w/
      exponential backoff, tool retry, max-iteration guard, refusal handling
- [x] 5 tools with pydantic validation (`agent/tools.py`): lookup_customer,
      get_order, check_refund_eligibility, process_refund (defense-in-depth
      revalidation + confirmation gate), escalate_to_human
- [x] Provider-agnostic LLM layer (`llm/`): Anthropic (default,
      `claude-opus-5`, prompt caching, effort param), OpenAI, offline mock
- [x] Reasoning-log event bus + SSE (`events.py`, `api/chat.py`, `api/admin.py`)
- [x] Frontend (no build step, served by FastAPI): chat w/ live status, voice
      (Web Speech STT + TTS), demo-customer picker, admin dashboard (stat tiles,
      filterable reasoning log w/ JSON payloads, refunds, escalations, CRM,
      policy), split view
- [x] Token streaming + typing indicator (added 2026-08-09) — see "Streaming"
      below; verified live against DeepSeek
- [x] 137 passing tests (`make test`): every policy rule, ledger defense in
      depth, agent-loop retry/failure paths via scripted fake LLM, streaming
      (delta reassembly + transient-event contract), HTTP API E2E
- [x] Docs: README.md, docs/architecture.md, docs/demo-script.md, .env.example

Remaining (owner: Sadiq, manual):
- [ ] Optional: paste a free ElevenLabs key into `.env` for spoken replies
      (`ELEVENLABS_API_KEY`). Works without one — falls back to browser speech.
      Verify with `curl localhost:8000/api/voices` and pick a voice ID.
- [x] **DeepSeek credits purchased (2026-08-08)** — `.env` configured for
      `deepseek-v4-flash`. This supersedes the earlier "free tiers only"
      constraint. Groq/Gemini/Ollama remain documented fallbacks in `.env`.
- [x] All 15 scenarios verified live on DeepSeek: correct outcome, rules,
      amount and side effects; 0 errors, 0 retries. Sweep harness:
      scratchpad/run_all_scenarios.py (recreate if useful)
- [ ] Do one full live run-through in the browser UI before recording
- [ ] Record the Loom video (`docs/demo-script.md` is the script)
- [ ] Create the public GitHub repo, commit, push (commands in README/handoff)

## How to run

```bash
make setup     # venv + deps   (PYTHON=python3.12 make setup if python3 is old)
make dev       # http://localhost:8000  (API + frontend, one process)
make test      # 137 tests
make lint      # ruff: lint + import order (add --fix to apply)
```

Config lives in `.env` at the repo root (see `.env.example`). The pill in the
app's top-right shows the active provider/model and warns if the key is missing.

## Architecture in one paragraph

`POST /api/chat` → `AgentRunner.run_turn()` (backend/app/agent/loop.py) runs
the loop: LLM call → tool calls → tool results → … → final reply. Providers
(backend/app/llm/) translate a small normalized message format to each vendor's
wire format, so the loop is provider-agnostic. Tools (backend/app/agent/tools.py)
validate args with pydantic and call deterministic services — most importantly
the policy engine (backend/app/services/policy.py), the ONLY place refund
eligibility/amounts are computed. Every step emits a typed `AgentEvent` to the
in-process bus (backend/app/events.py); the chat endpoint streams the current
turn's events, `/api/admin/stream` streams everything with replay. The frontend
(frontend/) is dependency-free ES modules served statically by the same app.

## Key decisions & rationale (don't undo casually)

1. **Raw function calling, no LangGraph/CrewAI** — deliberate: transparency of
   the loop is a demo talking point, and the assignment allows it explicitly.
2. **LLM never computes money or eligibility** — the policy engine decides;
   `process_refund` re-validates at execution time (`services/refunds.py`).
   This is the "holding the line" mechanism. Keep it.
3. **SDK auto-retries disabled** (`max_retries=0` in providers) — retries are
   implemented in the loop ON PURPOSE so they appear in the reasoning log
   (assignment asks to show failure/retry handling).
4. **Zero-build frontend** — this machine had NO Node.js/npm (Homebrew exists
   at /opt/homebrew; python3.12 came from it). Do not introduce a bundler
   without checking Node exists. The API is clean; a React swap is possible.
5. **Voice = Web Speech STT + ElevenLabs TTS with browser fallback.**
   STT is browser-native (frontend/js/voice.js). TTS goes
   frontend `speak()` → `POST /api/tts` (api/voice.py) → services/tts.py →
   ElevenLabs; the key is server-side only. Any failure (503 no key / 502
   provider error / blocked autoplay) silently falls back to
   `speechSynthesis`, so voice never breaks a demo. Added 2026-08-06 to hit
   the assignment's named-service bonus using ElevenLabs' free tier.
6. **Mock provider is dev/test-only** and every reply is prefixed
   "[mock mode]" so it can't be mistaken for a real demo. It streams its
   replies word by word, so the streaming UI can be developed and demoed
   offline with no key and no spend.
7. **Static files sent with Cache-Control: no-store** (NoCacheStaticFiles in
   main.py) — stale-asset bugs during demos are worse than re-fetches.
8. **Seed dates are day-offsets**, hydrated in `services/crm.py`. Never replace
   with absolute dates or the scenarios rot.
9. **The OpenAI adapter doubles as the universal OpenAI-compatible client**
   (`OPENAI_BASE_URL` → Groq / Gemini compat endpoint / Ollama). Added because
   Sadiq uses free-tier LLMs only. Compat endpoints get `max_tokens`; real
   OpenAI gets `max_completion_tokens`; localhost endpoints need no key.

## Streaming (added 2026-08-09)

Replies stream token by token; a typing indicator covers the gap before the
first token. Five design points, none of them safe to undo casually:

1. **`LLMProvider.complete_streaming()` has a working default** that calls
   `complete()` and delivers the text as one delta. That is why adding
   streaming did not touch a single existing test: the scripted fakes in
   `test_agent_loop.py` only implement `complete()` and inherit the rest. A
   provider that can't stream degrades to "text arrives at once", never breaks.
2. **Deltas are `transient=True` on the event bus** (`events.py`): fanned out
   to live subscribers, never appended to history. One reply is ~100-750
   fragments; storing them would evict the real reasoning trace from the
   5000-event bounded log within a couple of turns. `api/admin.py` also drops
   them from the admin SSE stream. Verified: 3 streamed turns → 27 events in
   history, zero deltas.
3. **Streamed text is provisional until `agent_response`.** The model may
   stream a working note and *then* call a tool. `frontend/js/chat.js` keeps
   streamed text in a throwaway bubble and discards it on `llm_request`,
   `llm_retry`, or `tool_call`; only `agent_response` commits (and re-renders
   through `renderMarkdownLite`). Streaming uses `textContent` — partial
   markdown renders as garbage, and it keeps model output out of `innerHTML`.
4. **Gemini is excluded from streaming** (`_NO_STREAM_HOSTS`). Streamed
   responses don't expose the provider's original tool-call object, so
   `ToolCallRequest.raw` would be None and Gemini's `thought_signature` lost —
   the exact bug that cost hours before. It falls back to the non-streaming
   path automatically.
5. **`stream_options={"include_usage": True}` only goes to known hosts**
   (`_STREAM_USAGE_HOSTS`). Without it a streamed turn reports no token usage;
   with it on an endpoint that doesn't know the parameter (local Ollama) the
   call can 400. DeepSeek/Groq/OpenAI accept it — usage still shows in the
   admin dashboard.

Turn `LLM_STREAMING=false` in `.env` to compare behaviour; the reply text is
identical either way (locked by a test).

## Conventions

- Python 3.12, pydantic v2, type hints everywhere; module docstrings explain
  the "why". Tests colocated in `backend/tests`, run with plain pytest
  (no pytest-asyncio; async code is tested via `asyncio.run`).
- Event types are the string constants in `events.py::EventType` — frontend
  badge/filter maps (frontend/js/admin.js, chat.js) must stay in sync with them.
- Money: floats rounded via `policy._money()`; fine for a demo, use Decimal if
  this ever becomes real.
- The demo persona table lives in THREE places to keep in sync if seed data
  changes: customers.json (`demo_notes`), README.md table, docs/demo-script.md.

## Gotchas for future sessions

- `.env` is configured for DeepSeek; `.env.groq.bak` holds the working Groq
  config, and `.env` comments carry verified recipes for Groq, Gemini and
  Ollama. `LLM_PROVIDER=mock` remains available for offline plumbing work.
- **Test count is 137.** Suites: policy rules, policy edge cases/boundaries,
  refund ledger, agent loop, agent-loop edge cases, providers, TTS, API.
- **Lint config is `ruff.toml` at the repo root** (added 2026-08-08 because
  VS Code's Ruff extension was flagging import blocks). `make lint` is clean —
  keep it that way. Note `ruff --fix` rewrites files, so re-read before editing.
  Four decisions carry the reasoning:
  1. `known-first-party = ["app", "tests"]` — without it Ruff files our own
     packages as third-party and interleaves `from app.config import ...`
     with pytest/fastapi.
  2. `line-length = 120` — the code was written at that width; 100 would flag
     ~25 pre-existing lines in policy.py without improving any of them.
  3. The rule set is pinned explicitly (E,W,F,I,UP,B,SIM,C4,PIE,RUF,DTZ,BLE).
     **This matters:** with no config the extension falls back to Ruff's own
     defaults, which changed in 0.16 to a much broader set — that mismatch is
     what made the editor light up while `make lint` looked clean.
  4. Four findings are suppressed in code with a justification comment, not by
     weakening the config: `BLE001` on the two deliberate catch-alls
     (`loop.py`, `tools.py` — they degrade errors instead of crashing a turn)
     and `DTZ011` on `date.today()` in `crm.py`/`policy.py` (a consumer
     "30 days after delivery" window is local calendar time; UTC would shift
     the boundary). RUF001-003 are ignored globally — the prose uses en dashes.
  - Gotcha: don't start a comment with the word "noqa" — Ruff parses it as a
    blanket directive and then flags it as unused (RUF100).
- `LLM_EFFORT=low` is intentional (snappy live demo). Raise to `medium`/`high`
  if tool-selection quality ever looks off, at some latency cost.
- The `.claude/launch.json` preview config exists but the sandboxed preview
  runner could not access ~/Desktop on this machine; run the server via
  terminal (`make dev`) instead.
- In-memory state resets on restart — that's a feature for repeated demo takes
  (fresh ledger every recording).
- `anthropic` SDK ≥0.120 / `openai` ≥2.53 are pinned loosely in
  backend/requirements.txt; both adapters normalize stop reasons and raise
  `TransientLLMError` for retryable failures only.
- **Groq free-tier facts (measured 2026-08-06 with Sadiq's key):**
  llama-3.3-70b = 12k TPM, gpt-oss-120b = 8k TPM, 1000 requests/day; TPM
  pre-validation counts `max_tokens`, hence `LLM_MAX_TOKENS=1024` in `.env`
  (16k caused HTTP 413 on every call). Rate-limit retries honor Retry-After
  (capped 30s). Groq 400 `failed_generation` (model emits malformed tool call
  — seen once with llama) is treated as transient and retried.
- **Full 15-persona live sweep (2026-08-06, gpt-oss-120b): 15/15 correct.**
  Every policy path produced the expected outcome, rules, and amount, with 0
  run errors. Two scenarios needed a second turn because the agent (correctly)
  asked for the return reason / electronics condition before calling the
  engine — protocol working as designed, noted in docs/demo-script.md.
  Sweep harness: scratchpad/run_all_scenarios.py (recreate if useful).
- **Audit findings (2026-08-06), all fixed — don't regress these:**
  1. Duplicate SKUs in `item_ids` double-counted the refund (e.g.
     `["GLV-030","GLV-030"]` → $50 instead of $25). `policy._select_items`
     now dedupes; `test_edge_cases.py` locks it in.
  2. Two TTS tests depended on the developer's real `.env` key; the fixture
     now blanks `elevenlabs_api_key` so tests are hermetic.
  3. `httpx` was filed under "Testing" in requirements.txt but is a runtime
     dep (services/tts.py).
  4. `.env.example` suggested `LLM_MAX_TOKENS=16000`, which 413s on Groq's
     free tier; now carries a warning.
- **Verified NOT broken** (don't "fix"): R1 day-30 and R3 day-7 windows are
  inclusive, R8 treats exactly $400.00 as auto-approvable — all match the
  policy wording ("up to 30 days", "within 7 days", "up to $400.00").
- **Desktop-only by decision (Sadiq, 2026-08-06).** Do not add mobile or
  responsive breakpoints; the only media query is the original 1100px one in
  admin.css. A narrow-viewport audit was reverted at his request.
- **Token diet (2026-08-06) — why the prompt looks the way it does.** A
  4-iteration turn was costing ~15.4k tokens against gpt-oss-120b's 8k TPM, so
  EVERY turn hit two rate-limit retries and took ~50s. Three fixes, all still
  in place — don't undo them without re-measuring:
  1. `prompts.py` builds the rule reference from `policy.RULES` (one line per
     rule) instead of embedding `refund_policy.md` verbatim: 1713 → ~1270 est.
     tokens. The full doc is still the authority, served at /api/admin/policy.
  2. `tools._portable_schema()` strips pydantic `title` keys and flattens
     `anyOf: [X, null]` → X. Saves ~160 tokens/call AND makes the schemas
     acceptable to Gemini, whose function-calling schema subset rejects anyOf.
  3. `LLM_MAX_TOKENS=400` (Groq counts max_tokens BEFORE running the request).
- **PROVIDER DECISION (2026-08-08): DeepSeek `deepseek-v4-flash` is the demo
  default.** Sadiq bought DeepSeek credits, which removes the free-tier
  ceilings that dominated a long investigation. Measured limits, all verified
  by pushing each provider until it errored and reading the message:

  | Provider | Real ceiling | ≈ agent turns |
  |---|---|---|
  | DeepSeek v4-flash (paid) | none | unlimited, <$0.01/turn |
  | Groq, per model | 100k tokens/DAY | ~11 (4 models = 4 budgets) |
  | Gemini free | 20 requests/DAY | ~5 |
  | Ollama local | none | unlimited, needs OLLAMA_CONTEXT_LENGTH=16384 |

  DeepSeek extras: 1M context, thinking mode on by default (5-10s/turn), and
  automatic prompt caching that hit ~97% on our static prefix. Its
  hold-the-line phrasing was the best of every model tried. All 15 scenarios
  verified correct on it, 0 errors, 0 retries.
- **Don't trust per-minute rate-limit headers as the whole story.** Groq's
  `x-ratelimit-remaining-tokens` showed 11,959/12,000 while the account was
  actually out of its 100k DAILY budget — the daily dimension is invisible in
  headers and only appears in the 429 body. This cost hours; read the error
  message, not the headers.
- **Persona guard added because llama leaked implementation.** Un-prompted it
  said "I'm a large language model" and "our deterministic policy engine" in
  the hold-the-line reply. `prompts.py` now has a "Voice — stay in character"
  section banning that vocabulary. Verified clean afterwards. Keep it.
- **Dead-end guard added because DeepSeek trailed off.** It ended a turn on
  "Thanks, I've found your order." with no question and no action, leaving the
  customer stuck. The Style section now requires every reply to end with a
  question, a stated action, or the outcome. Verified 8/8 clean runs across
  the two scenarios that need a follow-up question. Keep it.
- **Provider-quirk fixes in `llm/` — each found by running the scenario suite
  against a new provider. Don't regress these:**
  1. `ToolCallRequest.raw` holds the provider's original tool-call object and
     `_to_wire` replays it verbatim. Rebuilding from name+arguments dropped
     Gemini's `thought_signature`, which its thinking models require back —
     every multi-tool turn failed with a 400 until this landed.
  2. `_retry_after_seconds()` reads the `Retry-After` header AND parses
     "retry in 32.8" out of the body, because Gemini sends no header.
  3. `_rate_limit_detail()` keeps the provider's message ("tokens per day
     (TPD): Limit 100000, Used 98687") instead of logging bare
     `RateLimitError`. This is the single most useful diagnostic here.
