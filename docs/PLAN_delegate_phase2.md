# PLAN — `spendguard delegate` PHASE 2 : make it leverageable by OTHER users

Continue on branch `delegate-2hop` (the engine is committed at `delegate_router.py` / `codex_exec.py` sandbox param /
`cli.py` delegate command). Read the engine FIRST and use its REAL symbols: `delegate_router.delegate_task`,
`classify_task`, `_route_agentic`, `_model_for_plan`, `_measured_claude_overage_ratio`, `delegate_cli`. Same CLAUDE.md
hard rules (no hardcoding, decisions agentic not regex, read files whole, unique semantic names, no shortcuts, split
billed-$ from est-value never summed, offline tests set their own fake key). **Make ONE commit at the end** (the
honestreview pre-commit gate is costly per commit — do not commit incrementally).

## Item 2 — expose delegate as an MCP tool (`spendguard_delegate`)  [the key piece for in-conversation use]
- Add ONE tool to `mcp_server.py` (follow the existing ~19-tool registration + docstring style exactly; unique name).
- Signature mirrors the CLI: `spendguard_delegate(task, files=None, intent=None, provider="auto", execute=False)`.
- `execute=False` (DEFAULT) = dry-run: it CALLS `delegate_router.delegate_task(..., execute=False)` and returns its
  structured dict {status, classification, estimate}. `execute=True` passes through to run it.
- Tool description states the honest boundary verbatim: *offloads a SELF-CONTAINED subtask to another provider's plan;
  it does NOT reroute the calling agent's own turns.* Mention dry-run is default and names the est $ saved vs overage.
- Offline test: the MCP tool dry-run returns the structured plan and spends nothing (stub `delegate_task`).

## Item 3 — graceful lane auto-detection + `doctor` surfacing  [the "zero-config for any user" piece]
- New fn in `delegate_router.py` (unique name, e.g. `delegation_lanes_ready()`): returns which configured plans are
  actually usable here — a configured `advisor.lane_models` plan whose `lane_registry` exec/CLI is installed AND
  whose lane is reachable. REUSE the existing reachability machinery (what `spendguard health` / the lane probes
  already use); do NOT invent a second probe or hardcode a provider list.
- `delegate_task` routes ONLY to ready plans. If classification picks an unavailable plan → a typed message naming the
  READY alternatives (deliberate refusal — never fail-open into the Claude call the user is avoiding). If NO plan is
  ready → say exactly how to add one (install + auth that provider CLI).
- `spendguard doctor` gains a line like `delegation lanes ready: codex, gemini (2) · not configured: kimi, zai`
  (extend gate._cli's doctor output, same style). 
- Offline tests: lanes_ready with stubbed availability; route-to-unavailable → typed refusal; doctor line renders.

## Item 4 — docs / onboarding
- README: a `spendguard delegate` section — the 2-hop, the CLI + the MCP tool, WHEN to use it (plan capped / on
  overage → offload self-contained subtasks to a $0 or cheaper plan), and the honest boundary.
- `spendguard delegate --help`: complete, with examples (dry-run default, --yes, --provider, --files).
- Reference the existing §8 spendguard-rule so the behavioral guidance travels with the package.

## Also — consolidate the duplications honestreview flagged on the engine commit (only where GENUINELY 1:1)
- `delegate_router._model_for_plan` vs `lane_catalog.lane_model_for_tier`: if the tier→model resolution is truly the
  same contract, REUSE `lane_catalog.lane_model_for_tier` (adapt kind→tier at the call site) and delete the divergent
  copy. If the semantics differ (normalization/validation), leave `_model_for_plan` and add a one-line comment saying
  why it is not the same function.
- `cli.py:_opt` / `_ropt` vs `lanes.py` option helpers: if 1:1, extract ONE shared helper and call it from both;
  otherwise leave and note why. Do not force a merge that changes behavior.

## Verification (must be green before you finish — ONE commit)
- `spendguard deploy` gate: ruff + chunked pytest (475+ files) + name-uniqueness + `spendguard audit --ci`, all green.
- New offline tests pass; no live spend in the suite.
- Do NOT push / no PR. Commit once on `delegate-2hop` with a clear message; end with a summary of files changed, the
  new MCP tool name, the doctor line, and gate results.
