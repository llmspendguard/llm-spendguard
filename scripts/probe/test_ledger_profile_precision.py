"""Offline regression fixtures for the Part A attribution contract."""

import importlib.util
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

LEDGER_PRECISION_HARNESS_PATH = Path(__file__).with_name("ledger_warm_daemon_cpu_profile.py")
LEDGER_PRECISION_SPEC = importlib.util.spec_from_file_location(
    "ledger_precision_harness", LEDGER_PRECISION_HARNESS_PATH)
LEDGER_PRECISION_HARNESS = importlib.util.module_from_spec(LEDGER_PRECISION_SPEC)
LEDGER_PRECISION_SPEC.loader.exec_module(LEDGER_PRECISION_HARNESS)
LEDGER_PRECISION_SOURCE = LEDGER_PRECISION_HARNESS_PATH.parents[2] / "src"
LEDGER_PRECISION_PS = "PID TID STAT\n1 101 R\n1 202 S\n"
LEDGER_PRECISION_SLEEPING_STEP = (
    "Call graph:\n  1 Thread_101\n  + 1 wait\n  1 Thread_202\n  + 1 sqlite3_step + 10\n")
LEDGER_PRECISION_RUNNING_STEP = (
    "Call graph:\n  1 Thread_101\n  + 1 sqlite3_step + 10\n  1 Thread_202\n  + 1 wait\n")
LEDGER_PRECISION_FOREIGN_IDENTITY = ("spendguard/unrelated.py", "_ensure_schema")
LEDGER_PRECISION_QUERY_IDENTITY = ("spendguard/ledger.py", "SpendLedger._lock_date")
LEDGER_PRECISION_UNKNOWN_IDENTITY = ("spendguard/ledger.py", "SpendLedger.unattributed")


def ledger_precision_fixture_frame(identity, parent=None):
    path, qualname = identity
    return SimpleNamespace(f_code=SimpleNamespace(
        co_filename=str(LEDGER_PRECISION_SOURCE / path), co_qualname=qualname), f_back=parent)


class LedgerPrecisionFixtures(unittest.TestCase):
    def test_ledger_precision_pins_exist_in_source(self):
        identities = (LEDGER_PRECISION_HARNESS.LEDGER_PROFILE_SETUP_IDENTITIES
                      | LEDGER_PRECISION_HARNESS.LEDGER_PROFILE_QUERY_IDENTITIES)
        for relative_path, qualname in identities:
            tree = ast.parse((LEDGER_PRECISION_SOURCE / relative_path).read_text())
            parts = qualname.split(".")
            nodes = tree.body
            for part in parts:
                matches = [node for node in nodes if isinstance(
                    node, (ast.FunctionDef, ast.ClassDef)) and node.name == part]
                self.assertEqual(len(matches), 1, (relative_path, qualname))
                nodes = matches[0].body

    def test_scoped_ledger_precision_identities(self):
        for identity in LEDGER_PRECISION_HARNESS.LEDGER_PROFILE_SETUP_IDENTITIES:
            with self.subTest(identity=identity):
                self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
                    ledger_precision_fixture_frame(identity), LEDGER_PRECISION_SOURCE), "ledger_setup")
        foreign = ledger_precision_fixture_frame(LEDGER_PRECISION_FOREIGN_IDENTITY)
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
            foreign, LEDGER_PRECISION_SOURCE, True), "other")
        query = ledger_precision_fixture_frame(LEDGER_PRECISION_QUERY_IDENTITY)
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
            query, LEDGER_PRECISION_SOURCE), "other")
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
            query, LEDGER_PRECISION_SOURCE, True), "query")
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
            ledger_precision_fixture_frame(LEDGER_PRECISION_UNKNOWN_IDENTITY),
            LEDGER_PRECISION_SOURCE, True), "other")
        foreign.f_back = query
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_frame_category(
            foreign, LEDGER_PRECISION_SOURCE, True), "other")

    def test_ledger_precision_thread_intersection(self):
        self.assertFalse(LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
            LEDGER_PRECISION_PS, LEDGER_PRECISION_SLEEPING_STEP))
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
            LEDGER_PRECISION_PS, LEDGER_PRECISION_RUNNING_STEP), {101})
        self.assertEqual(LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
            LEDGER_PRECISION_PS, LEDGER_PRECISION_RUNNING_STEP.replace("Thread_101", "Thread_0x65")),
            {101})
        with self.assertRaises(RuntimeError):
            LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
                "PID STAT\n1 R\n", LEDGER_PRECISION_RUNNING_STEP)
        with self.assertRaises(RuntimeError):
            LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
                LEDGER_PRECISION_PS, "sqlite3_step")
        with self.assertRaises(RuntimeError):
            LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
                LEDGER_PRECISION_PS, LEDGER_PRECISION_SLEEPING_STEP.replace(
                    "Thread_202", "Thread unknown"))
        with self.assertRaises(RuntimeError):
            LEDGER_PRECISION_HARNESS.ledger_profile_running_step_threads(
                LEDGER_PRECISION_PS + "1 101 S\n", LEDGER_PRECISION_RUNNING_STEP)

    def test_ledger_precision_sandbox_grants_only_configured_port(self):
        rendered = LEDGER_PRECISION_HARNESS.SANDBOX_TEMPLATE.format(port=4912)
        network = [line for line in rendered.splitlines() if "network" in line]
        self.assertEqual(len(network), 1, rendered)
        # Only the daemon's own port is reachable on loopback; no wildcard to a sibling local service.
        self.assertIn("localhost:4912", network[0])
        self.assertNotIn("localhost:*", rendered)
        self.assertNotIn(":*", network[0])

    def test_ledger_precision_cpu_partition(self):
        phases = {"ledger_setup": 2, "query": 3, "other": 5, "query_native_seconds": 3}
        events = [{"event": "cpu", "threads": {"fixture": phases}},
                  {"event": "observation_end", "at_monotonic": 1}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "telemetry.jsonl").write_text("\n".join(map(json.dumps, events)))
            LEDGER_PRECISION_HARNESS.ledger_profile_summarize(output)
            split = json.loads((output / "accounting.json").read_text())["central_cost_split"]
        self.assertAlmostEqual(sum(split[key] for key in (
            "setup_thread_cpu_fraction", "query_thread_cpu_fraction", "other_thread_cpu_fraction")), 1)
        self.assertEqual(split["other_thread_cpu_fraction"], 0.5)


if __name__ == "__main__":
    unittest.main()
