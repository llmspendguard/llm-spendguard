# Fleet update propagation

Installing a new wheel, changing an editable checkout, or promoting the green pointer does not rewrite Python objects already loaded into a running process. A long-lived consumer continues executing the modules it imported at startup until it exits and imports them again. `served_sha()` therefore freezes the first observed source commit for the life of the process, while `green_pointer()` is read live; refreshing the served SHA would incorrectly describe the checkout rather than the code in memory ([`src/spendguard/release.py:69`](../src/spendguard/release.py#L69), [`src/spendguard/release.py:84`](../src/spendguard/release.py#L84)).

## Registry and stale detection

`register_consumer()` records the current PID, package version, frozen served SHA, dirty flag, command line, optional launchd label, and registration time in `fleet.db`. Registration is once per process and deliberately never raises onto the LLM call path ([`src/spendguard/fleet.py:29`](../src/spendguard/fleet.py#L29), [`src/spendguard/fleet.py:72`](../src/spendguard/fleet.py#L72)). `adapters.call()` invokes it on entry, and daemon entry points may register explicitly, so consumers become visible when they actually use spendguard ([`src/spendguard/adapters.py:932`](../src/spendguard/adapters.py#L932)).

`running_consumers()` removes dead PIDs by default and compares each live consumer's frozen `served_sha` with the current green SHA. The result is:

- `stale: true` when both SHAs are known and differ;
- `stale: false` when both are known and equal;
- `stale: null` when the comparison cannot be established, for example with no green pointer or a non-Git/pinned install.

Unknown is not treated as stale, preventing restart loops based on incomplete evidence ([`src/spendguard/fleet.py:117`](../src/spendguard/fleet.py#L117), [`src/spendguard/release.py:98`](../src/spendguard/release.py#L98)). `spendguard fleet` displays this view; `spendguard fleet --json` emits it as JSON ([`src/spendguard/cli.py:319`](../src/spendguard/cli.py#L319)).

## Restart and self-exit

`restart_stale(apply=False)` is a dry run. For each confirmed-stale consumer, it resolves a launchd label and reports the `launchctl kickstart -k` it would issue. With `apply=True`, launchd-managed consumers are kickstarted and allowed to respawn onto current code. An unsupervised stale process is never killed because spendguard cannot guarantee it will return; it is placed in the `warn` list for manual restart. Consumers with unknown version status are also warnings, not restart targets ([`src/spendguard/fleet.py:149`](../src/spendguard/fleet.py#L149)).

A supervised daemon can instead call `release.exit_if_stale()` periodically or between units of work. It exits only if `should_respawn()` proves both that the loaded code is stale and that a new process would start from the green SHA. This live-HEAD check prevents an exit loop when the checkout and green pointer differ. Unsupervised processes must not use this helper ([`src/spendguard/release.py:109`](../src/spendguard/release.py#L109), [`src/spendguard/release.py:123`](../src/spendguard/release.py#L123)). The MCP stdio server performs the equivalent handoff after flushing a response, between requests, so no in-flight request is dropped ([`src/spendguard/mcp_server.py:936`](../src/spendguard/mcp_server.py#L936)).

## Deploy integration

`spendguard deploy` holds a non-blocking single-instance deploy lock, runs the full gate unless `--no-gate` is supplied, and promotes only after the gate succeeds. The green pointer names committed code; dirty promotion requires the explicit `--allow-dirty` escape hatch ([`src/spendguard/cli.py:285`](../src/spendguard/cli.py#L285), [`src/spendguard/release.py:145`](../src/spendguard/release.py#L145)).

After promotion, deploy calls `restart_stale(apply=True)` by default. It prints each launchd restart and emits warnings for stale consumers it cannot safely restart. `--no-restart` suppresses this fleet step ([`src/spendguard/cli.py:300`](../src/spendguard/cli.py#L300)). This yields the operational sequence:

1. Gate and promote the new green SHA.
2. Compare registered consumers' loaded SHAs with green.
3. Restart launchd-managed stale consumers.
4. Warn about unsupervised or uncomparable consumers so an operator can restart or inspect them.
5. Let supervised consumers using `exit_if_stale()` hand off at their next safe point.

Promotion alone completes only step 1. A deployment is not fully propagated until stale long-lived consumers have restarted or safely self-exited.
