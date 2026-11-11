#!/usr/bin/env bash
set -euo pipefail

successful_run_count="$(gh api --method GET --paginate "repos/${GITHUB_REPOSITORY}/actions/workflows/ci.yml/runs" -f head_sha="$GITHUB_SHA" -f status=completed --jq '[.workflow_runs[] | select(.head_sha == env.GITHUB_SHA and .conclusion == "success")] | length' | awk '{total += $1} END {print total + 0}')"
if [ "$successful_run_count" -lt 1 ]; then
  echo "::error::no successful ci.yml run exists for tagged commit $GITHUB_SHA"
  exit 1
fi
echo "reused $successful_run_count successful ci.yml proof(s) for $GITHUB_SHA"
