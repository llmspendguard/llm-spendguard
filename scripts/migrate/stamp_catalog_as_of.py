import spendguard; spendguard.require()
"""Derive missing catalog row dates from semantic Git history. Dry-run unless ``--write`` is supplied."""
import argparse
import datetime
import json
import os
import subprocess
import sys

from spendguard import config, model_catalog

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CATALOG_REL = "src/spendguard/model_catalog.json"


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def _revision_catalog(commit):
    try:
        return (json.loads(_git("show", f"{commit}:{CATALOG_REL}")) or {}).get("models") or {}
    except (subprocess.CalledProcessError, json.JSONDecodeError, AttributeError):
        return None


def derive_dates(current, today=None):
    """Return ``{id: {date, reason}}`` by replaying parseable catalog revisions oldest-to-newest."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    lines = [line.split("\t", 1) for line in _git(
        "log", "--reverse", "--format=%H%x09%cI", "--", CATALOG_REL).splitlines() if "\t" in line]
    last_payload, result, newest = {}, {}, {}
    for commit, authored_at in lines:
        models = _revision_catalog(commit)
        if models is None:
            continue
        newest = models
        for mid, rec in models.items():
            payload = model_catalog.tracked_catalog_payload(rec)
            if mid not in last_payload or payload != last_payload[mid]:
                result[mid] = {"date": authored_at[:10],
                               "reason": "first appearance" if mid not in last_payload else "tracked payload changed"}
            last_payload[mid] = payload
    proposals = {}
    for mid, rec in current.items():
        if rec.get("as_of"):
            proposals[mid] = {"date": rec["as_of"], "reason": "existing", "kind": "existing"}
        elif mid in newest and model_catalog.tracked_catalog_payload(rec) == model_catalog.tracked_catalog_payload(newest[mid]):
            prior = result.get(mid)
            proposals[mid] = ({**prior, "kind": "from-history"} if prior else
                              {"date": None, "reason": "no parseable semantic history", "kind": "unknown-legacy"})
        elif mid in newest:
            proposals[mid] = {"date": today, "reason": "working-tree tracked payload differs from history", "kind": "today"}
        else:
            proposals[mid] = {"date": None, "reason": "row absent from parseable history", "kind": "unknown-legacy"}
    return proposals


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write derived dates into only rows whose as_of is missing")
    ap.add_argument("--output", help="also write the complete dry-run table to this path")
    args = ap.parse_args(argv)
    with open(model_catalog.DATA_PATH) as fh:
        doc = json.load(fh)
    proposals = derive_dates(doc.get("models") or {})
    lines = ["model_id\tproposed_as_of\tprovenance\treason"]
    for mid in sorted(proposals):
        p = proposals[mid]
        lines.append(f"{mid}\t{p['date'] or '-'}\t{p['kind']}\t{p['reason']}")
    counts = {kind: sum(p["kind"] == kind for p in proposals.values())
              for kind in ("today", "from-history", "unknown-legacy", "existing")}
    lines.append("SUMMARY " + " ".join(f"{k}={v}" for k, v in counts.items()))
    if not args.write:
        lines.append("DRY-RUN: model_catalog.json unchanged")
        text = "\n".join(lines) + "\n"
        sys.stdout.write(text)
        if args.output:
            with open(args.output, "w") as fh:
                fh.write(text)
        return 0

    sys.stdout.write("\n".join(lines) + "\n")

    def _stamp(catalog):
        for mid, p in proposals.items():
            rec = (catalog.get("models") or {}).get(mid)
            if rec is not None and rec.get("as_of") is None and p["date"]:
                rec["as_of"] = p["date"]
        return catalog

    config.update_json(model_catalog.DATA_PATH, _stamp, reason="bootstrap semantic per-row catalog as_of", required=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
