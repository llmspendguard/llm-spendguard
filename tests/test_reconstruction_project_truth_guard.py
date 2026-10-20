"""Offline reconstruction attribution/truth guard, including publication failure."""
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from spendguard import adapters, ledger_sync, budget

RECON_GUARD_ROOT = Path(__file__).resolve().parents[1]
RECON_GUARD_SCRIPT = RECON_GUARD_ROOT / "scripts" / "reconstruct" / "realtime_find_batch.py"
RECON_GUARD_DAY = "2026-09-17"
RECON_GUARD_PROVIDER = "openai"
RECON_GUARD_PROJECTS = ("fixture-project-alpha", "fixture-project-beta", None)
RECON_GUARD_USD = 2.0
RECON_GUARD_TRUTH_USD = 11.0
RECON_GUARD_ORG = "fixture-org"

recon_guard_spec = importlib.util.spec_from_file_location("reconstruction_project_truth_fixture", RECON_GUARD_SCRIPT)
recon_guard_module = importlib.util.module_from_spec(recon_guard_spec)
recon_guard_spec.loader.exec_module(recon_guard_module)

with tempfile.TemporaryDirectory() as recon_guard_dir:
    recon_guard_home = Path(recon_guard_dir)
    recon_guard_state = recon_guard_home / "fixture-state"
    recon_guard_truth = recon_guard_home / "fixture-truth.json"
    recon_guard_rows = [dict(name=project, project=project, org=RECON_GUARD_ORG,
                             provider=RECON_GUARD_PROVIDER, model="gpt-5.5", day=RECON_GUARD_DAY,
                             usd=RECON_GUARD_USD, basis="printed", sessions=[str(index)], reasoning="whole evidence")
                        for index, project in enumerate(RECON_GUARD_PROJECTS)]
    Path(str(recon_guard_state) + ".result").write_text(json.dumps({"runs": recon_guard_rows}))
    recon_guard_truth_rows = [dict(day=RECON_GUARD_DAY, provider=RECON_GUARD_PROVIDER, usd=RECON_GUARD_TRUTH_USD)]
    recon_guard_truth.write_text(json.dumps(recon_guard_truth_rows))
    recon_guard_tags = {"text": json.dumps({"tags": [dict(idx=index, tag="REALTIME", inflated=False)
                                                   for index in range(len(recon_guard_rows))]})}
    with patch.object(recon_guard_module, "STATE", str(recon_guard_state)), \
         patch.object(recon_guard_module.config, "HOME", recon_guard_home), \
         patch.object(adapters, "call", return_value=recon_guard_tags), \
         patch.object(recon_guard_module.conv, "segments", return_value=[]), \
         patch.object(recon_guard_module.conv, "_seg_get_all", return_value={}), \
         patch.object(recon_guard_module.conv, "session_day_map", return_value={}), \
         patch.object(recon_guard_module.conv, "resolve", return_value={}), \
         patch.object(recon_guard_module.conv, "_is_spendguard_session", return_value=False):
        recon_guard_module.clean(truth_path=recon_guard_truth)
    recon_guard_cache_path = recon_guard_home / "realtime_reconstruction.json"
    recon_guard_cache = json.loads(recon_guard_cache_path.read_text())
    recon_guard_expected = [project or "unattributed" for project in RECON_GUARD_PROJECTS]
    assert [row["project"] for row in recon_guard_cache["rows"]] == recon_guard_expected
    assert recon_guard_cache["truth"] == recon_guard_truth_rows
    assert recon_guard_cache["provider_months"][0]["truth_total"] == RECON_GUARD_TRUTH_USD
    assert recon_guard_cache["provider_months"][0]["month"] == RECON_GUARD_DAY.rsplit("-", 1)[0]
    assert any(gap["kind"] == "unattributed" for gap in recon_guard_cache["gaps"])
    with patch.object(ledger_sync.config, "HOME", recon_guard_home):
        recon_guard_estimate = ledger_sync.realtime_reconstruction_estimate(since=RECON_GUARD_DAY)
    assert [row["project"] for row in recon_guard_estimate["by_project"]] == recon_guard_expected
    with patch.object(ledger_sync.config, "HOME", recon_guard_home), \
         patch.object(budget, "clear_reconciled"), \
         patch.object(budget, "record_reconciled") as recon_guard_record:
        ledger_sync.record_realtime_reconstruction(since=RECON_GUARD_DAY)
    assert [entry.kwargs["project"] for entry in recon_guard_record.call_args_list] == recon_guard_expected
    recon_guard_original = recon_guard_cache_path.read_bytes()
    with patch.object(recon_guard_module.os, "replace", side_effect=OSError("injected publication failure")):
        try:
            recon_guard_module._write_reconstruction_checkpoint(str(recon_guard_cache_path), {"replacement": True})
        except RuntimeError as recon_guard_error:
            assert "injected publication failure" in str(recon_guard_error)
        else:
            raise AssertionError("publication failure was swallowed")
    assert recon_guard_cache_path.read_bytes() == recon_guard_original
    assert Path(str(recon_guard_cache_path) + ".checkpoint").read_bytes() == recon_guard_original
print("reconstruction_project_truth_guard PASS")
