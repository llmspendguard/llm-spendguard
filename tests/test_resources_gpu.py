"""vast.ai GPU reconstruction — per-UTC-day cost split, snapshot → history, and live∪history merge so DESTROYED
instances (gone from the API) stay reconstructable. Pure given injected instance dicts; no network. Isolated home."""
import os, sys, tempfile, datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import resources

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# Anchor to a FIXED UTC midnight so the per-UTC-day split is exact regardless of wall-clock time or the host's
# timezone. gpu_rows_by_day buckets on UTC-midnight boundaries — with a real `time.time()` a 24h instance straddles
# two UTC days (no single $48 day) UNLESS `now` happens to be near UTC midnight. That made this test pass locally
# and FAIL on CI's clock. Whole-UTC-day instances off a midnight anchor are deterministic everywhere.
now = datetime.datetime(2026, 6, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
inst1 = {"id": 1, "gpu_name": "H100", "dph_total": 2.0, "start_date": now - 3 * 86400, "end_date": now - 2 * 86400, "label": "train-a"}
inst2 = {"id": 2, "gpu_name": "A100", "dph_total": 1.0, "start_date": now - 1 * 86400, "end_date": now, "label": "train-b"}
resources.instances = lambda: [inst1, inst2]

rows = resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now)
ck("gpu_rows_by_day → per-day rows", len(rows) >= 2)
ck("cost = dph × hours (inst1 ~ $48 over its 24h)", any(abs(r["cost"] - 2.0 * 24) < 2.0 for r in rows))
total = sum(r["cost"] for r in rows)
ck("total ≈ inst1 $48 + inst2 $24", abs(total - (48 + 24)) < 3.0)

# snapshot records both; then inst1 is "destroyed" (gone from the live API) → still reconstructed from history
resources.snapshot()
resources.instances = lambda: [inst2]
ids = {str(i.get("id")) for i in resources._all_instances()}
ck("destroyed instance reconstructed from snapshot history", ids == {"1", "2"})
rows2 = resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now)
ck("destroyed instance still in per-day rows", abs(sum(r["cost"] for r in rows2) - 72) < 3.0)

# label_map (config-driven) — empty default means no mis-attribution
ck("DEFAULT_LABEL_MAP empty (no opinionated defaults)", resources.DEFAULT_LABEL_MAP == [])
ck("unlabeled → no project (until user configures)", resources.project_of("train-a") == "")


# ── account-anchored, label-attributed reconcile (replaces the buggy conv-alignment gap-dump) ────────────────────
# _reconcile is PURE + deterministic: rows (project from instance LABEL) + account_total + conn + ptmap →
# {mine, captured, account_total, residual, by_org}. Properties tested: (1) this connection pushes ONLY its own
# project's boxes — a SHARED account can't leak cross-org; (2) every dollar traces to a labelled box (no fabricated
# flat $/day rows); (3) the unrecoverable remainder is an EXPLICIT residual, never dumped on a project; (4) residual
# → 0 when every box is captured (proves the process reconciles to the account given complete inputs).
ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}
rows = [
    {"day": "2026-06-10", "gpu": "A100 SXM4", "cost": 250.0, "instances": [1], "project": "lmm"},
    {"day": "2026-06-11", "gpu": "H100 SXM", "cost": 200.0, "instances": [2], "project": "lmm"},
    {"day": "2026-06-12", "gpu": "H200 NVL", "cost": 300.0, "instances": [3], "project": "manga2anime"},  # foreign org
    {"day": "2026-06-12", "gpu": "RTX 3090", "cost": 50.0, "instances": [4], "project": ""},               # unlabeled
]
rec = resources._reconcile(rows, 900.0, {"project": "lmm"}, ptmap)
ck("reconcile: mine = only THIS project's boxes (no cross-org leak)", {r["gpu"] for r in rec["mine"]} == {"A100 SXM4", "H100 SXM"})
ck("reconcile: mine all project=lmm", all(r["project"] == "lmm" for r in rec["mine"]))
ck("reconcile: mine sums to $450 (A100+H100)", round(sum(r["cost"] for r in rec["mine"]), 2) == 450.0)
ck("reconcile: by_org attributes by label", rec["by_org"].get("Healiom") == 450.0 and rec["by_org"].get("Ensight") == 300.0)
ck("reconcile: unlabeled box → (untagged), not a real org", rec["by_org"].get("(untagged)") == 50.0)
ck("reconcile: residual = account − Σ all boxes, explicit (900−800=100)", rec["residual"] == 100.0)

# full recovery: Σ boxes == account_total → residual 0 (the reconcile-to-account property)
ck("reconcile: residual → 0 when every box captured/recovered", resources._reconcile(rows, 800.0, {"project": "lmm"}, ptmap)["residual"] == 0.0)

# record_recovered: a box destroyed before snapshotting is reconstructable → flows through _all_instances
resources.instances = lambda: []                       # no live boxes; rely solely on recovered history
resources.record_recovered({"id": 99, "gpu_name": "H100 SXM", "dph_total": 3.61,
                            "start_date": now - 2 * 86400, "end_date": now - 1 * 86400, "label": "healiom_gpu_h100"})
ck("record_recovered: destroyed box enters _all_instances", "99" in {str(i.get("id")) for i in resources._all_instances()})

print(("\n[FAIL] " if fails else "\n[OK] ") + f"resources_gpu: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
