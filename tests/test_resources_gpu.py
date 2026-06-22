"""vast.ai GPU reconstruction — per-UTC-day cost split, snapshot → history, and live∪history merge so DESTROYED
instances (gone from the API) stay reconstructable. Pure given injected instance dicts; no network. Isolated home."""
import os, sys, tempfile, time

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

now = time.time()
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

print(("\n[FAIL] " if fails else "\n[OK] ") + f"resources_gpu: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
