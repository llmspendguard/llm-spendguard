"""Deterministic shard partitions for the isolated merge gate, resilient to an evolving test set.

Timing zero is a legitimate rounded measurement. A test the manifest has NOT yet timed (a newly added
one) is weighted at the median of the known durations and flagged loudly — it degrades shard BALANCE,
it never blocks the gate. Forgetting to record a new test's duration used to raise mid-collection and,
under `pytest -n auto`, surface as an opaque xdist worker INTERNALERROR; balance is not correctness, and
coverage completeness is enforced by the coverage gate, so an untimed test must never crash sharding.
The default unsharded runner does not read this file.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

GATE_SHARD_SCHEMA_VERSION = 1
GATE_SHARD_MANIFEST_FILENAME = ".test_durations.json"
GATE_SHARD_INDEX_ENV = "SPENDGUARD_GATE_SHARD_INDEX"
GATE_SHARD_TIMINGS_ENV = "SPENDGUARD_GATE_TIMINGS"
GATE_SHARD_REPORT_PATTERN = "gate-report-{index}.xml"
GATE_SHARD_COVERAGE_PATTERN = ".coverage.*"
GATE_SHARD_COVERAGE_INVENTORY = "gate-coverage-manifest.json"
GATE_SHARD_MINIMUM_COUNT = 1
GATE_SHARD_FIRST_INDEX = 0
GATE_SHARD_MANIFEST_FIELDS = frozenset(("schema_version", "shard_count", "durations"))
GATE_SHARD_UNSUCCESSFUL_XML_TAGS = frozenset(("failure", "error", "skipped"))


class GateShardEvidenceError(RuntimeError):
    """Timing, collection, or artifact evidence is missing or inconsistent."""


def _gate_shard_unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateShardEvidenceError(f"duplicate timing JSON key: {key}")
        result[key] = value
    return result


def validate_gate_shard_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != GATE_SHARD_MANIFEST_FIELDS:
        raise GateShardEvidenceError("invalid timing manifest fields")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != GATE_SHARD_SCHEMA_VERSION:
        raise GateShardEvidenceError("unsupported timing manifest schema")
    count = manifest["shard_count"]
    durations = manifest["durations"]
    if type(count) is not int or count < GATE_SHARD_MINIMUM_COUNT:
        raise GateShardEvidenceError("invalid configured shard count")
    if not isinstance(durations, dict) or len(durations) < count:
        raise GateShardEvidenceError("missing timing inventory or more shards than cases")
    for nodeid, duration in durations.items():
        if not isinstance(nodeid, str) or not nodeid:
            raise GateShardEvidenceError("invalid timing nodeid")
        try:
            valid_duration = (type(duration) in (int, float) and math.isfinite(duration)
                              and duration >= GATE_SHARD_FIRST_INDEX)
        except OverflowError as error:
            raise GateShardEvidenceError(f"duration exceeds finite measurement range: {nodeid}") from error
        if not valid_duration:
            raise GateShardEvidenceError(f"invalid duration for {nodeid}: {duration!r}")
    return manifest


def read_gate_shard_manifest(path):
    try:
        return validate_gate_shard_manifest(json.loads(
            Path(path).read_text(), object_pairs_hook=_gate_shard_unique_json_object))
    except (OSError, ValueError) as error:
        raise GateShardEvidenceError(f"cannot read complete timing manifest {path}: {error}") from error


def verify_gate_shard_partition(nodeids, partitions):
    expected = Counter(nodeids)
    actual = Counter(nodeid for partition in partitions for nodeid in partition)
    if any(count != GATE_SHARD_MINIMUM_COUNT for count in expected.values()) or actual != expected:
        raise GateShardEvidenceError("shard partition must contain every collected nodeid exactly once")


GATE_SHARD_UNTIMED_FALLBACK_SECONDS = 0.2  # weight for a collected test the manifest has not yet timed


def gate_shard_fallback_duration(durations):
    """The weight for an untimed test: the median of known durations, or a small floor if none are known.
    A median (not a mean) keeps one slow outlier from inflating every new test's assumed cost."""
    known = [value for value in durations.values() if math.isfinite(value)]
    if not known:
        return GATE_SHARD_UNTIMED_FALLBACK_SECONDS
    return float(statistics.median(known))


def distribute_gate_shard_nodeids(nodeids, manifest):
    validate_gate_shard_manifest(manifest)
    durations = manifest["durations"]
    if len(nodeids) != len(set(nodeids)):
        duplicates = sorted(name for name, count in Counter(nodeids).items() if count != GATE_SHARD_MINIMUM_COUNT)
        raise GateShardEvidenceError(f"collection contains duplicate nodeids: {duplicates}")
    # An untimed test (added since the manifest was recorded) is weighted at the median and flagged; a stale
    # manifest entry (a removed test) is simply not partitioned. Neither blocks the gate — see module docstring.
    fallback = gate_shard_fallback_duration(durations)
    untimed = sorted(set(nodeids) - set(durations))
    if untimed:
        sys.stderr.write(
            f"[gate-shard] {len(untimed)} collected test(s) absent from {GATE_SHARD_MANIFEST_FILENAME}; "
            f"weighting each at the median {fallback:.3f}s for balance. Record real timings with "
            f"`python tests/_gate_shard_plan.py refresh` so shards stay balanced: {untimed}\n")
    weight = lambda item: durations.get(item, fallback)  # noqa: E731 - local balance weight, not a decision
    partitions = [[] for _ in range(manifest["shard_count"])]
    totals = [float(GATE_SHARD_FIRST_INDEX) for _ in partitions]
    for nodeid in sorted(nodeids, key=lambda item: (-weight(item), item)):
        index = min(range(len(partitions)), key=lambda item: (totals[item], len(partitions[item]), item))
        partitions[index].append(nodeid)
        totals[index] += weight(nodeid)
        if not math.isfinite(totals[index]):
            raise GateShardEvidenceError("aggregate shard duration overflow")
    verify_gate_shard_partition(nodeids, partitions)
    return partitions


def gate_shard_workflow_matrix(manifest):
    validate_gate_shard_manifest(manifest)
    return list(range(manifest["shard_count"]))


def read_gate_shard_coverage_inventory(directory, inventory_name=GATE_SHARD_COVERAGE_INVENTORY):
    try:
        inventory = json.loads((Path(directory) / inventory_name).read_text(),
                               object_pairs_hook=_gate_shard_unique_json_object)
    except (OSError, ValueError) as error:
        raise GateShardEvidenceError(f"missing coverage inventory: {directory}") from error
    if not isinstance(inventory, dict) or not inventory:
        raise GateShardEvidenceError(f"empty coverage inventory: {directory}")
    return inventory


def verify_gate_shard_coverage_inputs(directory, inventory):
    from coverage import CoverageData
    from coverage.exceptions import CoverageException

    directory = Path(directory)
    files = list(directory.glob(GATE_SHARD_COVERAGE_PATTERN))
    if not files or {path.name for path in files} != set(inventory):
        raise GateShardEvidenceError(f"incomplete coverage file inventory: {directory}")
    shard_has_measured_branches = False
    for path in files:
        try:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != inventory[path.name]:
                raise GateShardEvidenceError(f"coverage file digest differs: {path}")
            data = CoverageData(basename=str(path))
            data.read()
            if data.measured_files():
                if not data.has_arcs():
                    raise GateShardEvidenceError(f"non-empty coverage partial lacks branch arcs: {path}")
                shard_has_measured_branches = True
        except (OSError, CoverageException) as error:
            raise GateShardEvidenceError(f"invalid coverage evidence: {path}: {error}") from error
    if not shard_has_measured_branches:
        raise GateShardEvidenceError(f"empty branch coverage union for shard: {directory}")


def snapshot_gate_shard_coverage(directory, inventory_name=GATE_SHARD_COVERAGE_INVENTORY):
    directory = Path(directory)
    inventory = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in directory.glob(GATE_SHARD_COVERAGE_PATTERN) if path.is_file()}
    verify_gate_shard_coverage_inputs(directory, inventory)
    (directory / inventory_name).write_text(json.dumps(inventory, sort_keys=True) + "\n")


def read_gate_shard_junit(path):
    """Read every JUnit case; reject missing, duplicated, or unsuccessful evidence."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise GateShardEvidenceError(f"cannot read gate report {path}: {error}") from error
    durations = {}
    for suite in root.iter("testsuite"):
        for field in ("failures", "errors", "skipped"):
            if suite.get(field, str(GATE_SHARD_FIRST_INDEX)) != str(GATE_SHARD_FIRST_INDEX):
                raise GateShardEvidenceError(f"unsuccessful gate report {path}: {field}")
        cases = list(suite.findall("testcase"))
        try:
            declared_count = int(suite.attrib["tests"])
        except (KeyError, ValueError) as error:
            raise GateShardEvidenceError(f"missing report case count {path}") from error
        if declared_count != len(cases):
            raise GateShardEvidenceError(f"incomplete report cases {path}")
        for case in cases:
            if any(child.tag in GATE_SHARD_UNSUCCESSFUL_XML_TAGS for child in case):
                raise GateShardEvidenceError(f"unsuccessful testcase in {path}")
            try:
                nodeid = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                duration = float(case.attrib["time"])
            except (KeyError, ValueError) as error:
                raise GateShardEvidenceError(f"incomplete testcase in {path}") from error
            if nodeid in durations:
                raise GateShardEvidenceError(f"duplicate report testcase {nodeid}")
            durations[nodeid] = duration
    if not durations:
        raise GateShardEvidenceError(f"empty gate report {path}")
    # Reuse validation, without inventing any positive floor for rounded times.
    validate_gate_shard_manifest({"schema_version": GATE_SHARD_SCHEMA_VERSION,
                                 "shard_count": GATE_SHARD_MINIMUM_COUNT, "durations": durations})
    return durations


def verify_gate_shard_artifacts(manifest, reports, artifact_prefix, inventory_name=GATE_SHARD_COVERAGE_INVENTORY):
    """Prove every shard passed and supplied coverage before coverage combine.

    The complete collection is the UNION of the shards' own reports — the ground truth of what actually
    ran — not the committed timing manifest. A test added since the manifest was last recorded is weighted
    at the median by distribute_gate_shard_nodeids (see its docstring), so recomputing the partition from
    the same live collection the shards used keeps this verifier consistent with runtime sharding, instead
    of refusing an otherwise-green suite because the timing manifest trails the test set.
    """
    reports = Path(reports)
    expected_names = {artifact_prefix + str(index) for index in gate_shard_workflow_matrix(manifest)}
    try:
        actual_names = {path.name for path in reports.iterdir()}
    except OSError as error:
        raise GateShardEvidenceError(f"missing shard artifact directory: {reports}") from error
    if actual_names != expected_names:
        raise GateShardEvidenceError(f"incomplete shard artifacts; expected={sorted(expected_names)} actual={sorted(actual_names)}")
    # First pass: read every shard's report. Their union is the collection that actually ran — the set
    # the runtime sharder (conftest) partitioned — which is what the expected partition must be built from.
    shard_reports = {}
    for index in gate_shard_workflow_matrix(manifest):
        directory = reports / (artifact_prefix + str(index))
        report = directory / GATE_SHARD_REPORT_PATTERN.format(index=index)
        shard_reports[index] = read_gate_shard_junit(report)
    collected = sorted({nodeid for report in shard_reports.values() for nodeid in report})
    partitions = distribute_gate_shard_nodeids(collected, manifest)
    observed = []
    for index, expected in enumerate(partitions):
        directory = reports / (artifact_prefix + str(index))
        actual = shard_reports[index]
        if set(actual) != set(expected):
            raise GateShardEvidenceError(f"shard {index} report differs from its complete assigned partition")
        verify_gate_shard_coverage_inputs(directory, read_gate_shard_coverage_inventory(directory, inventory_name))
        observed.append(list(actual))
    verify_gate_shard_partition(collected, observed)


def gate_shard_cli_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    matrix = commands.add_parser("matrix")
    matrix.add_argument("--timings", required=True, type=Path)
    snapshot = commands.add_parser("snapshot-coverage")
    snapshot.add_argument("--directory", required=True, type=Path)
    snapshot.add_argument("--inventory-name", default=GATE_SHARD_COVERAGE_INVENTORY)
    verify = commands.add_parser("verify-reports")
    verify.add_argument("--timings", required=True, type=Path)
    verify.add_argument("--reports", required=True, type=Path)
    verify.add_argument("--artifact-prefix", required=True)
    verify.add_argument("--inventory-name", default=GATE_SHARD_COVERAGE_INVENTORY)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--junit", required=True, type=Path)
    refresh.add_argument("--shard-count", required=True, type=int)
    refresh.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot-coverage":
            snapshot_gate_shard_coverage(args.directory, args.inventory_name)
        elif args.command == "refresh":
            manifest = validate_gate_shard_manifest({"schema_version": GATE_SHARD_SCHEMA_VERSION,
                "shard_count": args.shard_count, "durations": read_gate_shard_junit(args.junit)})
            args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        else:
            manifest = read_gate_shard_manifest(args.timings)
            if args.command == "matrix":
                print(json.dumps(gate_shard_workflow_matrix(manifest)))
            else:
                verify_gate_shard_artifacts(manifest, args.reports, args.artifact_prefix, args.inventory_name)
    except (GateShardEvidenceError, OSError) as error:
        parser.exit(status=GATE_SHARD_MINIMUM_COUNT, message=f"gate shard evidence gap: {error}\n")


if __name__ == "__main__":
    gate_shard_cli_main()
