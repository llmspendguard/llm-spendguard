"""Offline scheduling, complete timing evidence, and deliberate promotion checks."""
from collections import Counter
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from _gate_shard_plan import GATE_SHARD_MANIFEST_FILENAME, validate_gate_shard_manifest

PARALLEL_CHECK_ROOT = Path(__file__).resolve().parents[1]
PARALLEL_CHECK_RUNNER = PARALLEL_CHECK_ROOT / "scripts" / "test" / "chunked_suite.py"
PARALLEL_CHECK_NAMES = ("test_parallel_probe_pass.py", "test_parallel_probe_fail.py")
PARALLEL_CHECK_WORKERS = 2
PARALLEL_CHECK_CACHE_NAME = ".test_durations.local.json"


def verify_chunked_parallel_evidence():
    spec = importlib.util.spec_from_file_location("chunked_parallel_evidence_runner", PARALLEL_CHECK_RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = [str(root / name) for name in PARALLEL_CHECK_NAMES]
        cache = root / PARALLEL_CHECK_CACHE_NAME
        seen = []

        def execute_chunked_parallel_probe(path, _runner_budget_scale):
            seen.append(path)
            return int(path == files[-1]), "probe output", ""

        with (patch.object(runner, "_test_files", return_value=files),
              patch.object(runner, "_run_one", side_effect=execute_chunked_parallel_probe),
              patch.object(runner, "LOCAL_DURATION_CACHE", str(cache)),
              patch.object(runner, "REPO", str(root)),
              patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
              patch.dict(os.environ, {"SPENDGUARD_COVERAGE": "0"}),
              redirect_stdout(io.StringIO())):
            assert runner.main(["--chunk", "1/2"]) == 0
            assert not cache.exists()
            verdicts = []
            for options in (["--sequential"], ["--max-parallel", str(PARALLEL_CHECK_WORKERS)]):
                seen.clear()
                verdicts.append(runner.main(options))
                assert Counter(seen) == Counter(runner._test_files())
                evidence = json.loads(cache.read_text())
                manifest = runner._chunked_manifest_payload(evidence["durations"], PARALLEL_CHECK_WORKERS)
                validate_gate_shard_manifest(manifest)
                assert set(evidence["durations"]) == {
                    runner.CHUNKED_NODEID_TEMPLATE.format(Path(path).name) for path in files}
                assert evidence["coverage"] is False
            assert verdicts == [1, 1]
            original_cache = cache.read_bytes()
            assert runner.main(["--chunk", "1/2"]) == 0
            assert cache.read_bytes() == original_cache
            promote = ["--write-manifest", "--shard-count", str(PARALLEL_CHECK_WORKERS)]
            assert runner.main(promote) == 0
            validate_gate_shard_manifest(json.loads((root / GATE_SHARD_MANIFEST_FILENAME).read_text()))
            with patch.dict(os.environ, {"SPENDGUARD_COVERAGE": "1"}):
                assert runner.main(promote) == 1
            evidence["durations"].pop(next(iter(evidence["durations"])))
            cache.write_text(json.dumps(evidence))
            assert runner.main(promote) == 1
            cache.unlink()
            assert runner.main(promote) == 1
            with patch.object(runner, "_run_one", return_value=(0, "", "")):
                assert runner.main([]) == runner.main(["--sequential"]) == 0


verify_chunked_parallel_evidence()
print("Chunked parallel inventory, verdict parity, cache and promotion checks passed")
