# PLAN — 0.12.9: delegate agentic-readiness false-negative + warm codex daemon default

## Problem (measured live, 2026-10-06)
`spendguard_delegate` classified a task agentic→codex CORRECTLY, then REFUSED: "classifier selected unavailable plan
'codex'; ready alternatives: claude-code, gemini, zai-coding" — even though `codex exec` is authed and WORKING (it
built 0.12.7/0.12.8 and is building right now). Root cause: delegation readiness
(`delegate_router.delegation_lanes_ready`, 0.12.8 item 3) marks a plan ready/unavailable from the **$0 ONE-SHOT lane
probe** (`lanes.lanes_status`). But the AGENTIC route runs the provider's AGENT CLI (`codex_exec.run_prompt` →
`codex exec`), which is SEPARATELY authed. So a codex that can't serve the $0 one-shot lane (stale / needs re-login)
but CAN run `codex exec` is wrongly refused — defeating the tool's main purpose. Separately, the agent path
COLD-STARTS (>75s) when run raw, because the warm codex daemon (`codex_daemon.py`, persistent `codex mcp-server`,
>75s→~5s) is OPT-IN (`_daemon_enabled`) and the raw path doesn't use it.

## Fix (branch `delegate-agentic-readiness`, one commit, no push/PR, no deploy; CLAUDE.md hard rules)
1. **Split delegation readiness by ROUTE KIND.** Classify first (oneshot vs agentic), THEN check readiness for THAT
   route:
   - ONESHOT (→ `lane_balance.delegate`, a $0 one-shot lane): ready iff the $0 lane is reachable — today's
     `lanes_status` probe. Unchanged.
   - AGENTIC (→ the provider's agent CLI via `codex_exec.run_prompt` / the per-provider exec module): ready iff the
     AGENT CLI is present + authed — the SAME availability `codex_exec`/`_route_agentic` actually depends on (the
     codex binary resolvable + logged in). REUSE the existing codex availability/auth check `codex_exec` already uses
     (FIND it; do not invent a second probe). A codex-authed agent must NEVER be refused because the $0 one-shot lane
     is down.
2. **Warm daemon by default for the agentic path.** Route agentic codex through `codex_exec.run_prompt` (which already
   has the warm-daemon path), and make the daemon DEFAULT-ON (with an opt-out), so an agentic delegation is ~5s warm,
   not a >75s cold start. Spin up on first use, reuse, idle-timeout; honest fallback to a cold `exec` if the daemon
   fails (degrade, never break). If blanket default-on is too broad, default-on at least for the delegate agentic
   route.
3. Keep ensure-success + loud fallback intact: if the agent CLI is GENUINELY unavailable (not merely the $0 lane), the
   existing typed refusal + ready-alternatives behavior stands.

## Tests (offline, own fake key)
- Agentic codex readiness = READY when `codex exec` is authed even though the $0 one-shot lane probe reports DOWN
  (stub both) — i.e. the exact refusal observed today must NOT recur.
- Oneshot readiness still gates on the $0 lane probe (unchanged).
- The agentic route uses the daemon-capable `run_prompt` path (warm), with a clean fallback to a cold exec on daemon
  failure.
- Full gate green (ruff + chunked pytest + name-uniqueness + audit --ci). ONE commit.
