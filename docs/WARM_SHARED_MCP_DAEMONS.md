# Warm shared MCP daemons — program design

**Status:** proposed · **Owner:** spendguard fleet · **Scope:** cross-repo (spendguard + 7thsense + honestreview + symgrep + ccwatch)

One sentence: run **one long-lived, shared, self-healing HTTP MCP daemon per tool** that every session connects to,
instead of each session spawning its own stdio copy of every tool — which is what leaks memory, wastes cold-start
time, and strands stale code.

---

## 1. Problem

MCP servers today are **stdio, spawned per client connection**. A Claude or codex session that has N tools configured
starts N fresh server processes, and when the session (or the codex app-server driving it) ends, the set is frequently
**not reaped**. Three concrete failures observed in one session (2026-10-11):

- **Per-session codex leak.** A one-shot `codex exec` starts *every* `mcp_server` in `~/.codex/config.toml` (node_repl,
  7thsense, ccwatch, honestreview, symgrep, spendguard). At `--workers 4 × ~53s/call` a delegation run abandoned
  ~12 full 7-server sets/min — **~1.5 GB/min, hundreds of orphaned 0%-CPU procs** — until the driving process exited
  (RSS peaked ~32 GB). Fixed for *spendguard's* delegate path by disabling MCP on the cold exec path
  ([codex_exec.\_mcp_disable_flags], PR #40) — but that only addresses delegation, not interactive use.
- **Orphan accumulation.** `honestreview-mcp` reached **8+ simultaneous instances** across a day of sessions (stdio
  servers outliving their ended sessions); `spendguard mcp` similarly runs one-per-session.
- **Stale code → the recurring false "codex lane LOGGED OUT" toast.** A long-lived daemon loads spendguard once at
  startup and never reloads after a `spendguard deploy`, so it keeps running old auth-detection. Partially closed by
  wiring `release.exit_if_stale()` into the honestreview/7thsense request loops — but that is N processes to keep fresh,
  one per session, forever.

All three share **one root cause: the unit of an MCP server is the *session*, not the *tool*.**

### Warm-lane app-server orphan sweep

Warm codex/kimi workers carry a structural owner marker: lane plus the spawning owner's PID and process create time.
`spendguard reap-orphans` reports that marker population and distinct owners; `--execute` reaps only servers whose
recorded owner identity is provably dead. It is a dry run by default, accepts `--lane LABEL` and `--json`, and counts
cross-uid permission denials as proven non-candidates. Other unreadable processes remain explicit classification gaps;
their exact count and a bounded sample are reported. Unreadable is nonfatal; a failed reap exits 1, while every clean
scan or fully successful reap exits 0.

The periodic fleet job runs the executing JSON form even when no pool exists to run a health tick. Count workers from
the owner-marker population reported here, never from `ps | grep codex`: each codex worker is a two-process tree (the
Node `codex.js app-server` parent and native child), so command-line counting doubles the true worker population.

## 2. What already exists (extend, don't rebuild)

| piece | where | what it gives |
|---|---|---|
| warm codex app-server | `codex_daemon` (default ON) | one persistent `codex app-server`, startup paid once; MCP disabled via the global gate |
| HTTP MCP transport | 7thsense `mcp_server.run_http_async` (stateless, `POST /mcp`); honestreview `mcp_client` (stateless HTTP) | a server that MANY clients can hit with one `urllib` call — the basis for sharing |
| staleness self-heal | `release.exit_if_stale()` / `should_respawn()` | a process exits when its code is older than the green pointer so a supervisor respawns it fresh |
| single-instance guard | `config.single_instance_lock(name)` (flock) | one holder at a time — the basis for a singleton |
| fleet registry | `fleet.register_consumer` / `running_consumers` / `restart_stale` | who is running, which is stale, which is launchd-managed |
| green pointer | `release.promote_release` / `served_sha` | the authoritative "serve this commit" marker all gated code reads |

The groundwork is mostly present. The program is **wiring + a singleton + a supervisor**, not new servers.

## 3. Design

**Flip the unit from session to tool.** For each tool (7thsense, honestreview, symgrep, ccwatch, spendguard):

1. **One warm HTTP daemon, singleton.** Bind a fixed per-tool port (or a unix socket); a second start detects the
   running one (via `config.single_instance_lock` + a port probe) and **exits, leaving the incumbent**. The daemon runs
   `run_http_async` stateless, so every request is independent and any number of clients share it.
2. **Clients connect, never spawn.** Register each tool with Claude/codex as an **HTTP** server pointing at the shared
   URL (`http://127.0.0.1:<port>/mcp`), not as a `command:` that spawns stdio. One `.mcp-server.json` / `mcp add`
   change per tool. Codex's per-session MCP spawn then becomes a per-session **connect** (cheap, no process).
3. **Self-healing, once.** The daemon calls `release.exit_if_stale()` between requests; on a deploy it exits and the
   **supervisor** respawns it onto the green commit. One process to keep fresh, not hundreds.
4. **Supervised** by launchd (a `KeepAlive` agent per tool) so `fleet.restart_stale` can also kickstart it, and so an
   exit (staleness, crash) always respawns. Registered in the fleet so it is visible and version-tracked.
5. **Delegation stays MCP-free by default** (PR #40). A delegated task that genuinely needs tool X enables **only X**,
   pointed at X's warm daemon — never the full set, never a fresh spawn.

### The properties this buys, together
- **No leak** — one process per tool, not one set per session. Closes the codex-exec pile-up *and* the honestreview/
  7thsense orphan accumulation in one move.
- **Warm** — startup paid once; a new session's first tool call is a connect, not a cold start (the same lever that
  took delegated `codex exec` from >75s to ~5s).
- **Always fresh** — one supervised, self-healing process per tool; a deploy respawns it. The recurring stale-code
  toast cannot persist across a deploy.

## 4. Phases

- **P0 — singleton + supervisor (per tool).** Add the single-instance guard + an HTTP `serve` entrypoint + a launchd
  agent. Tool keeps working via stdio for clients not yet migrated. Guard: a second start exits; a deploy respawns.
- **P1 — client cutover.** Switch each tool's registration to the shared HTTP URL. Measure: session start no longer
  spawns the tool; one daemon serves all sessions. Keep stdio as a documented fallback.
- **P2 — fleet integration.** Register each warm daemon; `spendguard fleet` lists them with served-vs-green; a deploy's
  `restart_stale` kickstarts a stale one. Retire the per-session orphan-reaping workarounds.
- **P3 — selective delegation tools.** A delegated `codex exec` that needs tool X enables only X at its warm URL.

## 5. Invariants + guards (un-regressable, per CLAUDE.md)

- **Singleton:** starting a daemon whose port is already bound exits 0 and does not double-bind. *(test: second start is a no-op; one PID remains.)*
- **Shared-not-spawned:** a tool's client registration is an HTTP URL, not a spawning `command:`. *(test: the registration manifest carries a url, not a command.)*
- **Self-heal:** a request on a daemon older than the green pointer triggers `exit_if_stale` → respawn. *(already guarded for honestreview/7thsense request loops; extend to the daemon loop.)*
- **Delegation MCP-free by default:** cold `codex exec` carries `orchestrator.mcp.enabled=false`. *(test: PR #40, test_codex_exec.)*
- **Stateless safety:** every HTTP request is independent (no cross-request session state) so one daemon can serve
  concurrent clients. *(7thsense/honestreview already stateless; assert it.)*

## 6. Open decisions (for review)

1. **Supervisor:** launchd `KeepAlive` agents (survive logout/reboot, `restart_stale` can kickstart) vs a single
   spendguard-owned supervisor process. Recommendation: **launchd per tool** — it is the mechanism `restart_stale`
   already targets, so the fleet machinery works unchanged.
2. **Ports vs unix sockets.** Fixed localhost ports are simplest for codex/Claude HTTP MCP config; unix sockets avoid
   port collisions. Recommendation: **fixed localhost ports**, documented, one per tool.
3. **Discovery.** A tiny `spendguard mcp-fleet` that prints each tool's url + health (reusing `fleet_summary`), so a
   client config can be generated rather than hand-maintained.
4. **Blast radius.** A shared daemon is a shared failure domain — one bad deploy of tool X degrades X for every
   session. Mitigation: the green-pointer gate already blocks promoting a red commit; the daemon fails *open* to a
   cold stdio spawn if the warm URL is unreachable (availability never worse than today).

## 7. Non-goals
- Not changing what the tools *do* — only how they are hosted and reached.
- Not forcing warm daemons on delegated one-shot work — that stays MCP-free (PR #40).
- Not a new RPC/transport — reuse the existing stateless HTTP MCP contract.
