"""Stop a "resume" from silently RE-BUYING — or from silently reusing stale answers. The resumable-job guard.

TWO DIFFERENT FAILURES, WHICH DESERVE DIFFERENT ANSWERS
-------------------------------------------------------
spendguard resumes a job by CONTENT-ADDRESSED key: bulk_delegate's checkpoint hashes each unit's contents, a row is
written only after a call succeeds and parses, and a rerun skips any unit whose key is on disk. But a key says nothing
about HOW the answer was produced, and there are two ways a rerun can be wrong:

  PLAN DRIFT     the pack size, the item set or their order changed. Every content key changes, so NOTHING matches and
                 the job RE-BUYS IN FULL while looking completely normal. The only symptom is the bill.
                 -> hard refusal (JobPlanDrift). There is nothing to salvage: the recorded rows describe units that no
                    longer exist.

  METHOD DRIFT   the plan is identical but the PROMPT or the MODEL changed. Every key still matches, so the job happily
                 reuses answers produced by a method you have since changed.
                 -> NOT automatically fatal, and not automatically fine. A tightened instruction may be exactly why you
                    are re-running; a cosmetic rewording invalidates nothing. So it is ADJUDICATED by a handler the
                    caller supplies, and the strongest option MEASURES rather than guesses.

Treating method drift as fatal would throw away every answer already bought over a prompt tweak; treating it as
harmless would silently mix answers from two methods in one output. So the three fingerprints are hashed SEPARATELY —
one combined digest would collapse both failures into "something changed" and force the harshest answer.

THE OPTIONS ON METHOD DRIFT (what a handler returns)
----------------------------------------------------
  reuse        accept the recorded answers and run only what is missing
  revalidate   re-run a SAMPLE of already-answered units under the new method and MEASURE how often it agrees with
               what is stored; reuse the rest only if agreement holds (the caller supplies the sampler, because only
               it can make a call — this module never spends)
  rebuy        discard the recorded answers and buy them again

`revalidate` is the honest default to reach for: it produces a NUMBER about the actual change instead of an opinion.

FAIL CLOSED, AND RAISE A TYPED REFUSAL (not sys.exit)
-----------------------------------------------------
A file with rows but no fingerprint is refused: "I cannot tell whether this matches" must never look like "this
matches". And because this is a spendguard LIBRARY, a refusal RAISES a typed SpendGateRefused subclass (JobPlanDrift /
JobMethodUndecided) that PROPAGATES through the gate's deliberate-stop machinery — never SystemExit, which would kill a
host process that only wanted to govern one job. A re-buy that would silently cost full price again IS a spend event,
so blocking it belongs in the refusal family alongside the gate's other deliberate stops.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from .gate import SpendGateRefused

# The marker row, written once as the first line of the output/checkpoint file. Readers that only want work rows
# ignore it, because it carries no "work_key".
FINGERPRINT_KEY = "_job_fingerprint"

PLAN, PROMPT, MODEL = "plan", "prompt", "model"
METHOD_PARTS = (PROMPT, MODEL)


class JobPlanDrift(SpendGateRefused):
    """The recorded job was built from a DIFFERENT PLAN (pack size / item set / order), so every content key would miss
    and resuming would silently RE-BUY the whole job at full price. A deliberate refusal (subclasses SpendGateRefused →
    propagates through the gate's stop machinery). Pass allow_replan=True to re-buy deliberately, or write to a new out."""


class JobMethodUndecided(SpendGateRefused):
    """The plan is unchanged but the PROMPT or MODEL drifted, and no handler was supplied to decide what that means —
    neither default is safe (reuse mixes two methods in one output; rebuy discards paid work over a reworded sentence),
    so the resume refuses rather than guess. A deliberate refusal (subclasses SpendGateRefused)."""


def _fp_digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=False, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def compute_job_fingerprint(task: str, pack: int, item_ids, prompt_sample: str = None, model: str = None) -> dict:
    """The three components a resume depends on, hashed SEPARATELY (plan drift is unrecoverable; method drift is a
    judgement — one combined digest would collapse both into "something changed" and force the harshest answer).

    `item_ids` must be in the ORDER they will be batched — order is part of the plan, since batch N holds items
    [N*pack : (N+1)*pack]. `prompt_sample` is a RENDERED prompt (what the model actually receives, including any
    instruction assembled at runtime), not the template source; pass the same representative unit every run so the
    digest is stable across runs that differ only in data. A component passed as None is OMITTED rather than hashed as
    null — a file stamped retrospectively records only what can be checked (the plan is recoverable from recorded work,
    the prompt a past run used is not), and `compare` counts an absent part as CHANGED, so a plan-only marker makes the
    next run adjudicate its method rather than silently claim the method matched."""
    parts = {PLAN: _fp_digest({"task": task, "pack": int(pack), "items": list(item_ids)})}
    if prompt_sample is not None:
        parts[PROMPT] = _fp_digest({"prompt": prompt_sample})
    if model is not None:
        parts[MODEL] = _fp_digest({"model": model})
    return parts


def read_fingerprint(path: str):
    """(fingerprint dict or None, number of work rows) found in an output/checkpoint file.

    Returns (None, n) when the file has work rows but no marker — written before this guard existed. The caller must
    treat that as UNKNOWN, never as agreement. A pre-existing marker in an older single-hash format is returned as
    {"plan": <hash>} so it still compares on the part it covered."""
    if not os.path.exists(path):
        return None, 0
    found, rows = None, 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if FINGERPRINT_KEY in record:
                found = record[FINGERPRINT_KEY]
            elif "_plan_fingerprint" in record:          # the earlier single-hash format
                found = {PLAN: record["_plan_fingerprint"]}
            elif record.get("work_key"):
                rows += 1
    return found, rows


def write_fingerprint(path: str, fingerprint: dict, detail: dict) -> None:
    """Append the fingerprint marker, unless an identical one is already there."""
    existing, _rows = read_fingerprint(path)
    if existing == fingerprint:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({FINGERPRINT_KEY: fingerprint, "detail": detail}) + "\n")
        handle.flush()


def compare_fingerprints(recorded: dict, current: dict):
    """(plan_changed, [method parts that changed]). A part the recorded fingerprint does not carry counts as CHANGED,
    not as matching — an older marker that never recorded the prompt cannot vouch for it."""
    if not recorded:
        return True, list(METHOD_PARTS)
    plan_changed = recorded.get(PLAN) != current.get(PLAN)
    method = [p for p in METHOD_PARTS if recorded.get(p) != current.get(p)]
    return plan_changed, method


def guard_resume(path: str, fingerprint: dict, detail: dict, allow_replan: bool = False,
                 on_method_change=None) -> str:
    """Decide whether a resume may proceed. Returns the action taken; RAISES a typed SpendGateRefused on refusal.

    Returns one of 'fresh', 'match', 'reuse', 'revalidated', 'rebuy'. Raises JobPlanDrift on plan drift (unless
    allow_replan), and JobMethodUndecided on method drift with no handler. Both are SpendGateRefused subclasses, so a
    consumer catches them with the same machinery as every other deliberate spend stop (and a CLI wrapper can turn one
    into an exit code) — this library never calls sys.exit.

    PLAN drift always refuses: the recorded keys describe units that no longer exist, so every one would miss and the
    job would re-buy in full. METHOD drift calls `on_method_change(parts, recorded, current, rows)` when supplied; it
    must return 'reuse', 'revalidate' or 'rebuy'. Without a handler it RAISES (defaulting to reuse would mix two
    methods; defaulting to rebuy would discard paid work over a reworded sentence). 'rebuy' deletes nothing — it
    returns the action so the caller can write to a different output and keep both results."""
    recorded, rows = read_fingerprint(path)
    if rows == 0:
        write_fingerprint(path, fingerprint, detail)
        return "fresh"

    plan_changed, method_changed = compare_fingerprints(recorded, fingerprint)

    if plan_changed:
        what = ("has no fingerprint (written before this guard existed)" if not recorded else
                f"was built from a DIFFERENT PLAN ({(recorded or {}).get(PLAN)} != {fingerprint.get(PLAN)})")
        if allow_replan:
            print(f"!! --allow-replan: {os.path.basename(path)} {what}.\n"
                  f"   Its {rows:,} recorded row(s) will NOT be reused; this run re-buys that work deliberately.",
                  file=sys.stderr)
            write_fingerprint(path, fingerprint, detail)
            return "rebuy"
        raise JobPlanDrift(
            f"REFUSING to resume: {path} {what}.\n"
            f"  It holds {rows:,} paid row(s) whose keys came from different batching, so every key would miss and "
            f"this run would silently RE-BUY the whole job at full price.\n"
            f"  current: {json.dumps(detail)}\n"
            f"  Restore the previous inputs/settings, pass allow_replan=True to re-buy deliberately, or use a new "
            f"output path to keep both results.")

    if not method_changed:
        return "match"

    # Same plan, different method: the keys still match, so the recorded answers ARE reusable — the question is whether
    # they are still RIGHT.
    print(f"!! {os.path.basename(path)}: the plan is unchanged but the {' and '.join(method_changed).upper()} changed "
          f"since its {rows:,} row(s) were bought.", file=sys.stderr)
    if on_method_change is None:
        raise JobMethodUndecided(
            f"  No handler was supplied to decide what the {' and '.join(method_changed)} change means, and neither "
            f"default is safe: reusing would mix answers from two methods in one output, and re-buying would discard "
            f"paid work over what may be a reworded sentence.\n"
            f"  Supply on_method_change=(reuse|revalidate|rebuy), pass allow_replan=True to re-buy, or use a new output.")

    action = on_method_change(method_changed, recorded, fingerprint, rows)
    if action not in ("reuse", "revalidate", "rebuy"):
        raise ValueError(f"on_method_change returned {action!r}; expected 'reuse', 'revalidate' or 'rebuy'")
    if action == "rebuy":
        print("  -> REBUY: the recorded answers are not reused. Write to a new output to keep them.", file=sys.stderr)
        return "rebuy"
    print(f"  -> {action.upper()}: the recorded answers stand.", file=sys.stderr)
    write_fingerprint(path, fingerprint, detail)
    return "reuse" if action == "reuse" else "revalidated"
