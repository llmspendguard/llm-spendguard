#!/usr/bin/env bash
# build_status.sh — $0 NON-LLM status for a codex-driven build. Calls NO model and NO spendguard;
# pure git / pgrep / grep / curl, so checking progress costs nothing and no LLM turn.
# Usage: scripts/build_status.sh --branch <feature> [--base <base, default main>] [--log <codex run log>] [--pypi <name==version>]
#   env fallbacks: BUILD_STATUS_BRANCH / BUILD_STATUS_BASE / BUILD_STATUS_LOG / BUILD_STATUS_PYPI
#   every section degrades gracefully: a missing input is skipped with a note, never a hard error.
set -uo pipefail

BRANCH="${BUILD_STATUS_BRANCH:-}"; BASE="${BUILD_STATUS_BASE:-main}"
LOG="${BUILD_STATUS_LOG:-}"; PYPI="${BUILD_STATUS_PYPI:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --base)   BASE="${2:-main}"; shift 2 ;;
    --log)    LOG="${2:-}"; shift 2 ;;
    --pypi)   PYPI="${2:-}"; shift 2 ;;
    -h|--help) grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)" >&2; shift ;;
  esac
done

echo "=== codex exec alive? ==="
if pgrep -f "@openai/codex-darwin-arm64/vendor" >/dev/null 2>&1; then
  echo "  RUNNING (a codex exec agent is active)"
else
  echo "  idle (no codex exec agent)"
fi

echo "=== commits on '${BRANCH:-?}' not in '${BASE}' ==="
if [ -n "$BRANCH" ] && git rev-parse --verify --quiet "$BRANCH" >/dev/null 2>&1; then
  count=$(git rev-list --count "${BASE}..${BRANCH}" 2>/dev/null || echo "?")
  echo "  ${count} commit(s) ahead of ${BASE}:"
  git log --oneline "${BASE}..${BRANCH}" 2>/dev/null | sed 's/^/    /'
else
  echo "  (no --branch, or branch not found — skipped)"
fi

echo "=== gate markers (from --log) ==="
if [ -n "$LOG" ] && [ -f "$LOG" ]; then
  markers=$(grep -aE "\[lint\] (ok|FAIL)|\[chunk [0-9]+/[0-9]+\] [a-zA-Z]+: [0-9]+/[0-9]+ passed|OK: [0-9]+/[0-9]+ files passed|FAIL: [0-9]+/[0-9]+ files|COMMIT REFUSED|\[[^]]+ [0-9a-f]{7,}\]" "$LOG" 2>/dev/null | tail -8)
  if [ -n "$markers" ]; then echo "$markers" | sed 's/^/    /'; else echo "    (no gate markers yet)"; fi
else
  echo "  (no --log, or file missing — skipped)"
fi

echo "=== PyPI (from --pypi name==version) ==="
if [ -n "$PYPI" ] && [ "$PYPI" != "${PYPI#*==}" ]; then
  name="${PYPI%%==*}"; ver="${PYPI##*==}"
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://pypi.org/pypi/${name}/${ver}/json" 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then state="published"; else state="not published / unknown"; fi
  echo "  ${name} ${ver}: HTTP ${code} (${state})"
else
  echo "  (no --pypi name==version — skipped)"
fi
