#!/usr/bin/env bash
# Phase 2 of the durable lane queue: install a PERIODIC drainer as a launchd agent.
#
# Every INTERVAL seconds it runs `spendguard lanes --drain` (foreground) — draining the durable lane_queue onto
# idle plan capacity at $0, then EXITING when the queue is empty. Deliberately PERIODIC, NOT `--drain --forever`:
# a short-lived drain never holds the ledger write-lock between runs (the stale-daemon-lock lesson — a long-lived
# gated consumer once held that lock for 31 days and dropped charges). launchd re-launches it each interval; if a
# run is still going when the next fires, launchd skips it (no overlap).
#
# Nothing machine-specific is hardcoded: the repo root is derived from this script's location, HOME from the env,
# and the local load ceiling is computed from the machine's physical core count. The lane CLIs (claude/codex/agy)
# are found by spendguard's own resolve_cli (pin -> PATH -> nvm/~/.local globs), so launchd's minimal PATH is fine.
# Idempotent: re-running reloads the agent.
#
#   scripts/lane/install_lane_drainer.sh [interval_seconds]      # default 300 (5 min)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"          # scripts/lane/ -> repo root
VENV_PY="$REPO/.venv.nosync/bin/python"
[ -x "$VENV_PY" ] || { echo "gated venv python not found at $VENV_PY — create/activate it first" >&2; exit 1; }

INTERVAL="${1:-300}"                                                # seconds between drains
SG_HOME="${SPENDGUARD_HOME:-$HOME/.spendguard}"
LOG="$SG_HOME/lane-drain.log"
LABEL="com.spendguard.lane-drain"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$SG_HOME" "$HOME/Library/LaunchAgents"

# Local load ceiling: pause leasing when the 1-min load exceeds ~80% of physical cores (piling subprocess lanes
# onto an already-thrashing box only makes it worse — the CPU-saturation case, distinct from lane saturation).
# Written to config so MANUAL `spendguard lanes --drain` honours it too, not just this agent.
CORES="$(sysctl -n hw.physicalcpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"
CEIL=$(( CORES * 4 / 5 )); [ "$CEIL" -ge 1 ] || CEIL=1
"$VENV_PY" -m spendguard.cli config set advisor.queue_load_ceiling "$CEIL" >/dev/null 2>&1 || \
  echo "note: could not set advisor.queue_load_ceiling — drain still runs; pass --ceiling manually" >&2

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_PY</string>
    <string>-m</string><string>spendguard.cli</string>
    <string>lanes</string><string>--drain</string>
  </array>
  <key>RunAtLoad</key><false/>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed $LABEL — every ${INTERVAL}s: spendguard lanes --drain  (load ceiling $CEIL cores, log $LOG)"
echo "  enqueue work:  spendguard lanes --enqueue <intent> --file tasks.txt"
echo "  status:        spendguard lanes --queue        watch: tail -f $LOG"
echo "  stop/remove:   launchctl unload $PLIST && rm $PLIST"
