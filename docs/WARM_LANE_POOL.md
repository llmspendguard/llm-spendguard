# Warm-lane exec pool — grounding + what was built

**Problem.** Every CLI-based subscription lane that spawns the provider CLI FRESH per call pays a full cold-start each
time (the CLI reloads its MCP schemas / plugins / agent base). Measured: a claude-code lane call cold-starts ~197K
cache-write tokens in production; codex cold `exec` measured >75s vs ~5s warm.

**The decisive requirement for a warm POOL.** Lane calls are *independent one-shot* prompts (classify file A, then
file B, …). A warm pool is only correct if it can start a FRESH, context-ISOLATED conversation per prompt inside ONE
resident process — so the expensive process/MCP setup is paid once while each prompt gets clean context (no
cross-prompt contamination, no per-prompt context growth). codex `app-server` has this via `thread/start`. A CLI
whose only "warm" mode is one *growing* conversation is NOT a pool — feeding it independent prompts would both
contaminate answers and grow cost per turn.

## Grounding verdict (per CLI, 2026-10-08)

| Lane | Warm pool of independent fresh-context prompts in one resident process? | Mechanism / flag evidence |
|---|---|---|
| **codex** | ✅ already shipped (`codex_daemon`) | `codex app-server` + `thread/start` per call |
| **kimi** | ✅ **built here** (`kimi_daemon`) | `kimi acp` = "Run kimi-code as an Agent Client Protocol (ACP) server over stdio". ACP `session/new` returns a `sessionId` and begins a clean session ("a new session starts clean; only session/load restores prior history"). One resident server, N× `session/new` → N isolated fresh contexts, cold-start paid once — the direct `thread/start` analogue. Confirmed live against the real CLI (initialize + session/new returned `sessionId`; a tiny warm prompt returned text), $0 billed. |
| **gemini (agy)** | ❌ **NO-GO — closed (structural), not built** | `--input-format stream-json` (embedded changelog in the `agy` binary): "reads newline-delimited JSON prompts from stdin and runs one turn per message in a **SINGLE conversation**" → one growing conversation, structural. The only fresh-context candidate, the `agy agentapi` *sidecar* (`new-conversation` per prompt), was GROUNDED (2026-10-08) and is a decisive NO-GO: that sidecar IS the **Antigravity GUI editor's LanguageServer** — there is NO `agy agentapi serve/start`, and run standalone every subcommand errors `ANTIGRAVITY_LS_ADDRESS is not set`. `agy` only *attaches* to an LS the Antigravity app launches and injects per-run env for (`ANTIGRAVITY_LS_ADDRESS`+`CSRF_TOKEN`+`SIDECAR_WEB_PORT`). **spendguard cannot spawn or own it** (codex/kimi are self-contained binaries spendguard spawns itself; this needs a running GUI editor + env-harvesting), and in every headless lane context (batch/comprehension/daemons/boxes/CI) no Antigravity editor runs → unviable. Also `--model` is a TIER (`flash_lite\|flash\|pro`), not a versioned id → a model-fidelity gap. Not proportionality — architecture. |
| **claude-code** | ❌ **no warm path — structural; not faked** | `claude -p --input-format stream-json --output-format stream-json` holds one process open, but every stdin user message appends to ONE growing conversation. The installed binary (v2.1.270) has control subtypes `initialize, interrupt, end_session, fork_conversation, rewind_conversation, …` but **no `start_session`/`reset_session`/new-conversation** subtype; `fork_conversation` COPIES history, `end_session` SHUTS DOWN the process, and subagents are parent-billed/model-decided and not addressable from stdin. `--resume`/`--continue` restart a fresh process and replay context (re-pay cold-start). So there is no in-process fresh-context pool. The only honest win is the already-shipped minimal cold-start cut (`subscription_exec._MINIMAL_COLD_START`: `--strict-mcp-config --mcp-config {} --setting-sources ""`, ~58% cache-write cut with OAuth login intact). A warm path was **deliberately not faked.** |

## What was built

- `warm_stdio_daemon._WarmStdioJsonRpcDaemon` — the SHARED concurrent line-delimited JSON-RPC-over-stdio transport +
  process lifecycle (one reader thread demuxing responses by `id`; write-lock-only concurrency so N turns stay in
  flight; serialised spawn with re-check; idle-timeout reclaim; dead-pipe fail-all; `recycle_on_timeout` hard-kill).
  Provider specifics are four overridable hooks (`_spawn_cmd`, `_handshake`, `_route_notification`, `_on_response`)
  plus each subclass's own `run_warm`. This replaces what would otherwise be drifting per-lane copies.
- `codex_daemon._CodexDaemon` — refactored to subclass the shared base (behaviour-preserving; the codex app-server
  fake-server + lifecycle tests are the regression gate).
- `kimi_daemon._KimiAcpDaemon` — the new ACP warm pool: initialize → `session/new` (fresh isolated session) →
  `session/prompt`, concatenating streamed `agent_message_chunk` text, terminating on the response's `stopReason`.
  A refusal/cancel `stopReason` is surfaced as a hard non-retryable `tool_error` (never recorded as content); a
  server-initiated request is declined so the turn never hangs.
- `kimi_exec.run_prompt` — warm-first with cold `-p` fallback (default-on, `SPENDGUARD_KIMI_DAEMON`/`advisor.kimi_daemon`
  opt-out). **Model fidelity:** `kimi acp` has no model flag (answers on `default_model`), so the warm path is used
  only when the requested model resolves to that default (or is unpinned); a specific other model takes the cold
  `-p -m <alias>` path, so the recorded model always matches the model that ran.

### Guards (tests)
- `tests/test_kimi_daemon_acp_server.py` (vs a fake ACP server): N prompts served by exactly ONE cold-start (not N);
  a wedged turn fails only itself while concurrent turns complete and the server survives; a refusal is a hard
  tool_error; a server-initiated request is declined (no hang); a dead pipe wakes every waiter.
- `tests/test_kimi_daemon.py`: `_spawn` degrades (never raises) on an unstartable binary; default-on + env opt-out;
  a warm-daemon failure falls back to cold `kimi -p`; model-fidelity eligibility gate.
- `tests/test_codex_daemon{,_app_server}.py`: unchanged, green — prove the shared-base refactor preserved codex.
- `scripts/probe/probe_kimi_acp_handshake.py`: live structural probe against the real `kimi acp` ($0; `--live-prompt`
  adds one tiny real turn).

## SCOPE

This makes the $0 subscription lanes genuinely cheap (valuable for batch/comprehension volume). It is NOT the lever
for overall "burning too fast": a prior reconciliation found the lane is only ~0.7% of total Claude-plan burn;
~99% is interactive context residency. Kept proportionate accordingly.

**Final state — the warm-pool work is COMPLETE, not partial.** Two lanes warm (codex + kimi) on the shared base;
the other two are CLOSED, not unfinished, each for a grounded reason: claude-code has no in-process fresh-context
primitive (structural), and gemini's only candidate (`agy agentapi`) depends on the Antigravity GUI editor's
LanguageServer that spendguard cannot spawn or own (architectural NO-GO — see the table). No future session should
treat claude-code or antigravity as a resume target.
