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


# ── account-gap reconcile guard: never dump a SHARED account's gap on this connection's org ──────────────────────
# Regression for the prod bug where manga2anime's destroyed-H200 gap ($900) landed on Healiom: the guard dropped
# empty-project instances, so a foreign/unlabeled box didn't mark the account as multi-project. sync() must only
# reconstruct the blanket gap when EVERY instance belongs to THIS connection's project.
from spendguard import saas, attribution
saas.conn = lambda: {"enabled": True, "visibility": "org", "owns_account": True, "project": "lmm",
                     "org": "Healiom", "contributor": "t@x.test", "url": "https://x", "api_key": "k"}
saas.contributor = lambda: "t@x.test"
saas._row_uid = lambda row: "uid_" + str(row.get("project")) + str(row.get("day")) + str(row.get("model"))[:6]
resources.snapshot = lambda: None
resources.account_gpu_total = lambda since_ts=None: 1000.0          # big account total → gap = 1000 - attributed
resources._month_start_ts = lambda: now - 30 * 86400
resources._gpu_alignment = lambda since: {("2026-06-10", "lmm"): {"w": 1.0, "org": "healiom", "team": "clinical-ai"}}
attribution.taxonomy = lambda: ({"orgs": ["Healiom"], "teams": [], "projects": []},)
attribution.project_team_map = lambda taxo: {"lmm": ("Healiom", "clinical-ai")}


def _recon_rows(payload):
    return [r for r in payload["day_totals"] if str(r.get("model", "")).startswith("(reconstructed")]


# CASE 1 — a foreign instance (manga2anime) is present → SHARED account → NO blanket-gap reconstruction.
resources.gpu_rows_by_day = lambda since_ts=None, now=None, label_map=None: [
    {"day": "2026-06-10", "gpu": "A100 SXM4", "cost": 250.0, "instances": [1], "project": "lmm"},
    {"day": "2026-06-12", "gpu": "RTX 3090", "cost": 5.0, "instances": [2], "project": "manga2anime"},  # FOREIGN
]
p1 = resources.sync(dry=True)
ck("shared account (foreign instance) → no reconstructed gap rows", _recon_rows(p1) == [])
ck("shared account → only this project's real instances pushed", all(r["project"] == "lmm" for r in p1["day_totals"]))

# CASE 2 — an UNLABELED instance (project '') is also foreign → still no reconstruction (the exact prod miss).
resources.gpu_rows_by_day = lambda since_ts=None, now=None, label_map=None: [
    {"day": "2026-06-10", "gpu": "A100 SXM4", "cost": 250.0, "instances": [1], "project": "lmm"},
    {"day": "2026-06-12", "gpu": "RTX 3090", "cost": 5.0, "instances": [2], "project": ""},  # UNLABELED → foreign
]
ck("unlabeled instance present → still no reconstructed gap (the prod miss)", _recon_rows(resources.sync(dry=True)) == [])

# CASE 3 — genuinely single-project account (every instance is this project) → gap IS reconstructed.
resources.gpu_rows_by_day = lambda since_ts=None, now=None, label_map=None: [
    {"day": "2026-06-10", "gpu": "A100 SXM4", "cost": 250.0, "instances": [1], "project": "lmm"},
]
ck("single-project account → gap reconstructed (this project's own destroyed boxes)", len(_recon_rows(resources.sync(dry=True))) > 0)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"resources_gpu: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
