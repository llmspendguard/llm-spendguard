"""saas.py pure transforms — the scrubbed /v1 payload builders extracted from the HTTP push (fetch→transform→load).
build_rollup_rows / build_guarded_rows / _project_filter / _conn_project_base take plain data + config and emit the
exact contract rows, with NO network/DB — so the filter, the kind/channel mapping, the $→micros, the contributor
stamping, and the scrubbing (only contract fields ever leave) are all unit-testable offline. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-saas-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import saas

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── _project_filter: None = all · list/single → set · owns_account widens to the shared llmseg/unattributed ──
ck("_project_filter: no project → None (push all)", saas._project_filter({}) is None)
ck("_project_filter: projects list → lowercased set", saas._project_filter({"projects": ["LMM", "SlideKit"]}) == {"lmm", "slidekit"})
ck("_project_filter: single project", saas._project_filter({"project": "Lmm"}) == {"lmm"})
ck("_project_filter: owns_account adds the shared llmseg + unattributed",
   saas._project_filter({"project": "lmm", "owns_account": True}) == {"lmm", "llmseg", "unattributed"})
ck("_conn_project_base: does NOT widen (guarded sources aren't shared)",
   saas._conn_project_base({"project": "lmm", "owns_account": True}) == {"lmm"})

# ── ORG-BASED filter: push EVERY taxonomy project under the connection's org (the agentic-attribution-safe path) ──
# Guards the bug that dropped concept-model/medical-taxonomy from the push because they weren't in a static list,
# AND the cross-org leak (llmseg is Ensight's — it must NOT ride along on a Healiom org connection).
from spendguard import attribution as _attr
_PT = {"lmm": ("Healiom", "clinical-ai"), "concept-model": ("Healiom", "clinical-ai"),
       "medical-taxonomy": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "engineering"),
       "llmseg": ("Ensight", "engineering")}
_attr.taxonomy = lambda *a, **k: ({}, {})            # org-mode reads project_team_map, not the raw taxonomy
_attr.project_team_map = lambda *a, **k: _PT
ck("org-mode: Healiom connection pushes EVERY Healiom project (concept-model/medical-taxonomy NOT dropped)",
   saas._project_filter({"org": "Healiom"}) == {"lmm", "concept-model", "medical-taxonomy"})
ck("org-mode: owner also absorbs 'unattributed' residual — but NOT 'llmseg' (that's Ensight's, no cross-org leak)",
   saas._project_filter({"org": "Healiom", "owns_account": True}) == {"lmm", "concept-model", "medical-taxonomy", "unattributed"})
ck("org-mode: Ensight connection gets ITS projects (incl llmseg), none of Healiom's",
   saas._project_filter({"org": "Ensight"}) == {"manga2anime", "llmseg"})
ck("org-mode: _conn_project_base is also org-based (guarded rows follow the same scope)",
   saas._conn_project_base({"org": "Healiom"}) == {"lmm", "concept-model", "medical-taxonomy"})
ck("org-mode: unknown/typo'd org → empty set = push NOTHING (fail-closed, never cross-org push-all)",
   saas._project_filter({"org": "Nonexistent"}) == set())

# ── crosscheck axis: _is_actual_row keeps the ACTUAL-$ axis, drops est-VALUE (claude-code/claude-ai, billed=false) ──
# Guards the false 'server_only' bug: crosscheck models actual-$ locally, so est-value rows must be excluded server-side
# (else in_sync is permanently False). `billed` flag wins when present; channel convention is the pre-billed fallback.
ck("is_actual: explicit billed=true → actual", saas._is_actual_row({"billed": True, "channel": "claude-code"}) is True)
ck("is_actual: explicit billed=false → NOT actual (flag beats channel)", saas._is_actual_row({"billed": False, "channel": "batch"}) is False)
ck("is_actual: no flag + batch/realtime → actual", saas._is_actual_row({"channel": "batch"}) and saas._is_actual_row({"channel": "realtime"}))
ck("is_actual: no flag + claude-code/claude-ai → est-value, excluded", not saas._is_actual_row({"channel": "claude-code"}) and not saas._is_actual_row({"channel": "claude-ai"}))
ck("is_actual: no flag + unknown channel → actual (don't silently drop)", saas._is_actual_row({}) is True)

# ── build_rollup_rows: filter + kind/channel map + $→micros + contributor stamp + uid + scrub ──
raw = [
    {"day": "2026-06-22", "provider": "openai", "model": "gpt-5.5", "kind": "batch", "cost": 2.5, "calls": 3, "project": "lmm"},
    {"day": "2026-06-22", "provider": "openai", "model": "o", "kind": "realtime", "cost": 1.0, "calls": 1, "project": "lmm"},
    {"day": "2026-06-22", "provider": "anthropic", "model": "opus", "kind": "meta", "cost": 0.5, "calls": 2, "project": "llmseg"},
    {"day": "2026-06-22", "provider": "openai", "model": "x", "kind": "batch", "cost": 9.0, "calls": 1, "project": "manga2anime"},
    {"day": "2026-06-22", "provider": "openai", "model": "(provider-batch)", "kind": "batch", "cost": 7.0, "calls": 0, "project": "unattributed"},
]
rows = saas.build_rollup_rows(raw, "alice@x.test", {"lmm", "llmseg", "unattributed"})
projs = [r["project"] for r in rows]
ck("rollup: filters to the connection's projects (manga2anime dropped)", "manga2anime" not in projs and "lmm" in projs)
lmm_batch = next(r for r in rows if r["project"] == "lmm" and r["channel"] == "batch")
ck("rollup: $→micros (2.5 → 2_500_000)", lmm_batch["spend_micros"] == 2_500_000)
ck("rollup: realtime kind→workload + channel realtime", next(r for r in rows if r["model"] == "o")["channel"] == "realtime")
ck("rollup: meta kind→meta", next(r for r in rows if r["project"] == "llmseg")["kind"] == "meta")
ck("rollup: contributor stamped on workload", lmm_batch["member_ref"] == "alice@x.test")
ck("rollup: unattributed gap carries NO contributor", next(r for r in rows if r["project"] == "unattributed")["member_ref"] == "")
ck("rollup: every row has a uid == _row_uid(row)", all(r["uid"] == saas._row_uid(r) for r in rows))
allowed = {"day", "provider", "model", "kind", "channel", "spend_micros", "calls", "member_ref", "project", "uid"}
ck("rollup: SCRUBBED — only contract fields leave (no prompt/content)", all(set(r) <= allowed for r in rows))
ck("rollup: flt=None pushes everything", len(saas.build_rollup_rows(raw, "a", None)) == len(raw))

# ── build_guarded_rows: filter (empty base = all) + cumulants pass through ──
grows = [
    {"day": "2026-06-22", "project": "lmm", "source": "cache", "n": 5, "k1": 10.0, "k2": 2.0, "k3": 0.1, "k4": 0.01},
    {"day": "2026-06-22", "project": "manga2anime", "source": "block", "n": 2, "k1": 4.0, "k2": 1.0, "k3": 0.0, "k4": 0.0},
]
g = saas.build_guarded_rows(grows, {"lmm"})
ck("guarded: filters to base (manga2anime dropped)", len(g) == 1 and g[0]["project"] == "lmm")
ck("guarded: cumulants pass through", g[0]["k1"] == 10.0 and g[0]["n"] == 5)
ck("guarded: empty base → push all", len(saas.build_guarded_rows(grows, set())) == 2)

# ── crosscheck robustness: a vast.ai outage must NOT turn correctly-pushed GPU rows into false server_only ──
# Regression guard for the flaky-fetch bug seen on 2026-06-23 (one run gave server_only=10, the re-run gave 0 /
# in_sync=True). When the GPU source is dark, server vastai rows are UNVERIFIED (couldn't check), never 'stale'.
from spendguard import resources as _res
saas.ready = lambda: (True, "")
saas._rollup_rows = lambda since=None: [{"uid": "batch1", "project": "lmm", "spend_micros": 1_000_000}]

# (A) GPU source DARK (sync(dry) empty + no instances): a local-only vastai server row → gpu_unverified, in_sync stays True
saas._request = lambda *a, **k: {"rows": [
    {"uid": "batch1", "project": "lmm", "provider": "openai", "kind": "workload", "channel": "batch", "spend_micros": 1_000_000, "billed": True},
    {"uid": "gpu1", "project": "lmm", "provider": "vastai", "kind": "gpu", "channel": "realtime", "spend_micros": 4_200_000, "billed": True},
]}
_res.sync = lambda dry=False: {"day_totals": []}
_res._all_instances = lambda: []
ccA = saas.crosscheck(since="2026-06-01")
ck("crosscheck: GPU source dark → vastai server row is gpu_unverified, NOT server_only",
   ccA.get("gpu_unverified") == 1 and ccA["server_only"] == 0)
ck("crosscheck: gpu_unverified alone does NOT flip in_sync (couldn't check ≠ drift)", ccA["in_sync"] is True and ccA["matched"] == 1)

# (B) DARK source but a genuinely-stale NON-gpu row is still flagged server_only (we don't suppress real drift)
saas._request = lambda *a, **k: {"rows": [
    {"uid": "batch1", "project": "lmm", "provider": "openai", "kind": "workload", "channel": "batch", "spend_micros": 1_000_000, "billed": True},
    {"uid": "stale1", "project": "lmm", "provider": "openai", "kind": "workload", "channel": "batch", "spend_micros": 500_000, "billed": True},
    {"uid": "gpu1", "project": "lmm", "provider": "vastai", "kind": "gpu", "channel": "realtime", "spend_micros": 4_200_000, "billed": True},
]}
ccB = saas.crosscheck(since="2026-06-01")
ck("crosscheck: real stale NON-gpu row still server_only even when GPU is dark",
   ccB["server_only"] == 1 and ccB.get("gpu_unverified") == 1 and ccB["in_sync"] is False)

# (C) GPU source OK → the GPU row is derived locally + matches; nothing unverified, in_sync True
_res.sync = lambda dry=False: {"day_totals": [{"uid": "gpu1", "project": "lmm", "spend_micros": 4_200_000}]}
_res._all_instances = lambda: [{"id": 1}]
saas._request = lambda *a, **k: {"rows": [
    {"uid": "batch1", "project": "lmm", "provider": "openai", "kind": "workload", "channel": "batch", "spend_micros": 1_000_000, "billed": True},
    {"uid": "gpu1", "project": "lmm", "provider": "vastai", "kind": "gpu", "channel": "realtime", "spend_micros": 4_200_000, "billed": True},
]}
ccC = saas.crosscheck(since="2026-06-01")
ck("crosscheck: GPU source OK → GPU row matches, none unverified, in_sync True",
   "gpu_unverified" not in ccC and ccC["matched"] == 2 and ccC["in_sync"] is True)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"saas_payload: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
