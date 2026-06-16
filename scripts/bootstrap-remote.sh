#!/usr/bin/env bash
# Bootstrap spendguard on a REMOTE / EPHEMERAL GPU box (e.g. a vast.ai H200) so its LLM/API calls are GATED,
# attributed to the right org/project, and pushed to the aggregation server. Ephemeral boxes start clean →
# ungated → spend leaks (account-billed but never captured). Bake this into the box's provisioning so every
# fresh box self-configures + SELF-VERIFIES (spendguard doctor).
#
# Secrets come from the environment — do NOT hardcode keys in this file or commit them. Pass at provision time:
#   OPENAI_API_KEY, ANTHROPIC_API_KEY   (provider keys, for reconcile/report)
#   SPENDGUARD_SAAS_KEY                 (the org/team INGEST key, e.g. the Manga2Anime org key sg_org_…)
# Optional overrides: PROJECT (default manga2anime), CONTRIBUTOR (default ash@ensight.ai), SERVER_URL.
#
# IMPORTANT — one provider account across machines: this box only PUSHES its own gate-attributed spend. It does
# NOT run `saas reconcile` (account-level reconciliation runs from ONE designated machine, so the account gap
# isn't re-attributed here). Use `spendguard saas push`, not `saas sync`, on remote boxes.
set -euo pipefail

: "${PROJECT:=manga2anime}"
: "${CONTRIBUTOR:=ash@ensight.ai}"
: "${SERVER_URL:=https://llm-spendguard-server.vercel.app}"
: "${SPENDGUARD_SAAS_KEY:?set SPENDGUARD_SAAS_KEY to this box's org/team ingest key}"

# 1. install the package (PyPI, or editable from a synced checkout)
pip install -q llm-spendguard 2>/dev/null || pip install -q -e "${SPENDGUARD_SRC:-$HOME/llm-spendguard}"

# 2. keys in the cwd-independent home so reconcile/report resolve them from any directory
mkdir -p ~/.spendguard
{ [ -n "${OPENAI_API_KEY:-}" ]    && echo "OPENAI_API_KEY=${OPENAI_API_KEY}";
  [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"; } > ~/.spendguard/.env
chmod 600 ~/.spendguard/.env

# 3. per-repo connection (push this box's project to its org; push-only, daily)
cat > ./.spendguard.json <<JSON
{ "enabled": true, "url": "${SERVER_URL}", "api_key": "${SPENDGUARD_SAAS_KEY}",
  "contributor": "${CONTRIBUTOR}", "project": "${PROJECT}", "visibility": "org", "sync_interval": "daily" }
JSON
chmod 600 ./.spendguard.json

# 4. gate this interpreter so calls are recorded (tagged project=$PROJECT)
spendguard install-hook --user --python "$(command -v python3)"

# 5. SELF-VERIFY — must show ENFORCING + keys resolved + push-as project=$PROJECT
spendguard doctor

echo "✓ spendguard ready on $(hostname): gates calls → project=${PROJECT} → ${SERVER_URL}"
echo "  after a run:  spendguard saas push    (push gate-attributed spend; do NOT 'saas reconcile' on remote boxes)"
