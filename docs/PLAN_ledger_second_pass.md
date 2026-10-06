# PLAN — second pass (items #5 + #6): lane quota capture + hard-$0 front doors

Second pass on branch `ledger-attribution-fixes` AFTER the 4-item build lands. Both items in ONE commit. Same
CLAUDE.md hard rules; no push/PR; additive only; honest "unknown" where a provider truly exposes nothing (never
fabricate a headroom).

## The gap (measured)
`spendguard lanes --usage` shows real headroom ONLY for claude-code (Anthropic exposes a quota surface):
`claude-code 🔴 0% left`. Every other lane is `quota unknown — no status surface / no call captured yet`
(codex / gemini / kimi / zai). So the pace/bandit router sheds OFF the capped claude-code lane onto the others
**blind to whether those lanes have room** — optimistic, not headroom-aware. If a target lane is also near its cap,
spendguard only finds out when a call falls over to metered (the measured $2.78 fallback this month).

## The fix — EXTEND the existing quota machinery, don't rebuild
The machinery already exists: `lane_quota.cached_usage(...)` and per-lane `usage()` (e.g. `codex_exec.usage()` at
codex_exec.py:270 already wraps it). claude-code's quota populates `lanes --usage`; the others don't. So:
- For EACH non-claude lane, capture remaining quota/headroom WHERE the provider/CLI exposes it, reusing
  `lane_quota.cached_usage` (TTL-cached, $0 where it reads a local status; a cheap probe only if that's the only
  surface — and only opportunistically, never a paid call just to read quota). Find what each exposes:
  - codex (ChatGPT plan): whatever `codex` surfaces for plan usage (there is already a `codex_exec.usage()` seam —
    wire its result into the quota view; if it returns nothing, keep unknown).
  - gemini (`agy`), kimi (`kimi`), zai (GLM plan): capture only if the CLI/endpoint exposes a usage/limit surface;
    otherwise leave an honest `unknown` (do NOT invent one).
- Surface the captured headroom in `spendguard lanes --usage` (replace "quota unknown — no status surface" with the
  real remaining % / reset when available; keep "unknown" only where the provider genuinely exposes none).
- Make shedding HEADROOM-AWARE: the pace/bandit lane ranking (lane_economics / route_utility / the pace weight) should
  prefer a ready lane with MORE measured remaining headroom over one near its cap — using the captured quota when
  present, and falling back to today's behavior (ready + pace) when a lane's quota is unknown. Do not regress the
  current routing when quota is unknown.

## Verification
- Offline tests: a lane whose provider exposes usage shows real headroom in `lanes --usage` (stubbed surface); a lane
  with no surface stays honest `unknown`; the ranker prefers the higher-headroom ready lane when quotas are known, and
  is unchanged when they're unknown. No paid call is made solely to read quota.
- Full gate green (ruff + chunked pytest + name-uniqueness + audit --ci). ONE commit covering BOTH #5 and #6.

## item #6 — ensure-success routing: exhaust $0 lanes, metered as a VISIBLE last resort (never silent)
PRINCIPLE (Ash, decisive — supersedes any "refuse-billed default-on"): ALWAYS deliver the intent — ensure success.
Prefer $0 lanes to save money, but if the ONLY way to succeed is metered $$, take it. NEVER fail a task to avoid
billing. So the $2.78 was NOT wrong for falling to metered (that ensured success); it was wrong for being SILENT, and
for likely falling to metered on the FIRST lane's miss when another $0 lane could have served it. gemini/`agy`
reliability is flagged in memory — exactly the lane that missed.

Fix, across EVERY lane front door (`comprehend`, `delegate`, `ask`):
- FUNGIBLE call (no exact-model pin): on a lane miss/down, prefer another $0 lane — but ONLY one whose MEASURED
  quality holds for the intent (reuse the best-value / route_utility quality signal; never a blind swap to a worse
  model). Exhaust the quality-equivalent $0 lanes before any metered fallback. Reuse the existing lane failover /
  on_miss machinery in `bulk_delegate` / the lane path — FIND it, do NOT reimplement.
- PINNED call (`no_substitution` / `metered_only` / an explicit `model_for`): NEVER substitute the model or lane. If
  its $0 lane misses, run the SAME model on its METERED API (the metered twin — same provider + model), ensuring
  success WITH the exact model. This is precisely the correct `metered_only` behavior the #2 audit enforces: a pinned
  fan keeps its exact model, metered, never a $0-lane/model swap. (A quality-equivalent $0 substitute is only ever for
  the fungible case above.)
- When metered fallback IS the only path to success, DO it (success is paramount) — but make it LOUD and ACCOUNTED,
  never silent: surface "fell over to metered $X — lanes <which> unavailable" in the command output, and record it
  with `fell_from` in the ledger (real-$ axis). A paid fallback must ALWAYS be visible, never a $2.78 surprise.
- Expose `--refuse-billed` as an EXPLICIT OPT-IN for the rare "$0-or-fail" case (a caller who would rather fail than
  bill — e.g. a strict free-only batch). It is NOT the default, and NOT auto-on when capped: the default ALWAYS
  ensures success.
- Offline tests: (a) a first-lane miss falls to ANOTHER $0 lane, not metered, when one is available (zero metered,
  task succeeds); (b) when ALL $0 lanes miss, it falls to metered, SUCCEEDS, and the fallback is surfaced + recorded
  with `fell_from`; (c) `--refuse-billed` opt-in → free error row, zero metered (the rare $0-or-fail path).
