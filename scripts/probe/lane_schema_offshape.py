"""Measure per-lane OFF-SHAPE + ITEM-DROP (arity) for a warden-shaped PACKED envelope, at $0 — via bulk_delegate.

Answers warden's Q1.4: does routing packed {results:[{id,label}]} work onto a fungible lane LOSE money (an off-shape
or short envelope → API price + a wasted lane round-trip) or hold? Rides the HARDENED path (dispatch governor,
content-keyed checkpoint/resume, per-task timeout) instead of hand-rolling a threadpool. The rails that make it safe:
  • refuse_billed=True  → a lane miss is a $0 error row, NEVER an API bill ($0 by construction).
  • lanes=<fungible>    → excludes the protected interactive lane (claude-code); measures only fungible lanes.
  • schema= + expect_ids= → the JSON contract rides EVERY lane's prompt (validated locally, strict-mode API on a
    miss), and the id-set arity check turns a shape-perfect-but-short envelope into a retried MISS, not a silent
    $0 success — so this measures exactly what a warden migration would experience.

Per lane it reports clean / arity-miss / other (bucketed by the ACTUAL adapter error string, not a pre-guessed
label). Staged by PACK SIZE (K) and per-item OUTPUT weight (LONG) — the two variables that stress completeness —
because a single easy K is the classic shortcut that returns a clean 0% pointing the wrong way.

The measurement RESULT is the report on stdout (the caller captures it). The checkpoint is bulk_delegate's durable
resume aid, written to a persistent out-of-repo runs dir (SPENDGUARD_HOME/probe_runs) so a re-run resumes and never
litters the repo; SG_CKPT_DIR overrides it. Run UNDER the gate. Est-value only (plan usage); $0 billed.

  SG_N_PER_LANE (default 15)  requests per lane (round-robin gives ~this many each)
  SG_K_ITEMS    (default 15)  items packed per request — the arity-stress lever; stage it UP
  SG_LONG_ITEMS (default 0)   1 → a required per-item `reason` (K sentences of output pressure) + longer inputs
  SG_CKPT_DIR   (default: SPENDGUARD_HOME/probe_runs/lane_offshape)  durable resume dir for this run
"""
import spendguard
spendguard.require()

import collections
import os
from spendguard import lane_balance

LANES = ["codex", "gemini", "zai-coding"]   # the fungible lanes (claude-code is protected — excluded via lanes=)
N_PER_LANE = int(os.environ.get("SG_N_PER_LANE", "15"))
K_ITEMS = int(os.environ.get("SG_K_ITEMS", "15"))
LONG_ITEMS = os.environ.get("SG_LONG_ITEMS", "0") == "1"
INTENT = "spendguard:lane-offshape"
_HOME = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
CKPT_DIR = os.environ.get("SG_CKPT_DIR") or os.path.join(_HOME, "probe_runs", "lane_offshape")  # durable, out-of-repo
os.makedirs(CKPT_DIR, exist_ok=True)
CKPT = os.path.join(CKPT_DIR, "ckpt_K%d%s.jsonl" % (K_ITEMS, "_long" if LONG_ITEMS else ""))

_item_props = {"id": {"type": "string"}, "label": {"type": "string"}}
_required = ["id", "label"]
if LONG_ITEMS:                                            # heavy: a per-item reason → K sentences of OUTPUT pressure
    _item_props["reason"] = {"type": "string"}
    _required.append("reason")
SCHEMA = {"type": "object", "required": ["results"],
          "properties": {"results": {"type": "array", "items": {
              "type": "object", "required": _required, "properties": _item_props}}}}
SYS = ('Classify each item\'s sentiment. For EVERY item id you are given, return exactly one object '
       '{"id": <the id>, "label": one of "positive"|"negative"|"neutral"'
       + ('} ' if not LONG_ITEMS else ', "reason": <one full sentence justifying the label>} ')
       + 'Return ALL ids, each exactly once, as {"results": [ ... ]}. Output JSON only, no prose.')
_BANK = ["love it", "works great", "best purchase yet", "exceeded expectations", "highly recommend",
         "broke immediately", "waste of money", "terrible support", "never again", "very disappointed",
         "arrived on time", "exactly as described", "standard packaging", "it is fine", "does the job"]
_LONG = ("I ordered this a few weeks ago after reading the reviews and comparing several alternatives, and my "
         "experience so far has been that %s — which honestly surprised me given the price point and the brand.")


def _task(req):
    ids = ["i%d_%d" % (req, j) for j in range(K_ITEMS)]

    def _phrase(j):
        p = _BANK[(req * 7 + j * 3) % len(_BANK)]
        return (_LONG % p) if LONG_ITEMS else (p + ".")
    body = "\n".join('%s: "%s"' % (ids[j], _phrase(j)) for j in range(K_ITEMS))
    return ids, "Classify these %d items:\n%s" % (K_ITEMS, body)


N_TASKS = N_PER_LANE * len(LANES)                        # round-robin over the arms → ~N_PER_LANE each
ids_by_task = {}
tasks = []
for req in range(N_TASKS):
    ids, t = _task(req)
    tasks.append(t)
    ids_by_task[t] = ids

stats = {}
print("dispatching %d packed tasks (%d items each%s) across %s, refuse-billed ($0)...\n"
      % (N_TASKS, K_ITEMS, ", +reason" if LONG_ITEMS else "", LANES), flush=True)
rows = lane_balance.bulk_delegate(
    tasks, INTENT, system=SYS, schema=SCHEMA,
    expect_ids=lambda t: ids_by_task[t],
    lanes=LANES, refuse_billed=True, force=True,        # force: a measurement I own (skips the bulk-gate/resilience prompt)
    checkpoint=CKPT, chunk_size=N_PER_LANE, deadline_s=180, stats=stats)

per = collections.defaultdict(lambda: {"clean": 0, "arity_miss": 0, "other": 0, "n": 0,
                                       "items_exp": 0, "items_drop": 0, "items_extra": 0, "items_dup": 0,
                                       "errs": collections.Counter()})
billed = 0
for row, t in zip(rows, tasks):
    lane = row.get("lane") or "?"
    d = per[lane]
    d["n"] += 1
    if row.get("billed"):
        billed += 1
    if row.get("text") and not row.get("error"):
        d["clean"] += 1
        d["items_exp"] += len(ids_by_task[t])           # a clean envelope = all ids present, 0 dropped
    elif row.get("arity_miss"):
        d["arity_miss"] += 1
        am = row["arity_miss"]
        d["items_exp"] += am.get("n_expected", len(ids_by_task[t]))
        d["items_drop"] += len(am.get("missing", []))
        d["items_extra"] += len(am.get("extra", []))
        d["items_dup"] += len(am.get("dupes", []))
    else:
        d["other"] += 1
        d["errs"][(row.get("error") or "?")[:80]] += 1

print("resumed %s · dispatched %s\n" % (stats.get("resumed"), stats.get("dispatched")))
print("=== PER-LANE OFF-SHAPE / ITEM-DROP  (refuse-billed → $0; ~%d req/lane × %d items%s) ==="
      % (N_PER_LANE, K_ITEMS, ", +reason" if LONG_ITEMS else ""))
for lane in sorted(per):
    d = per[lane]
    n = d["n"]
    usable = d["clean"]
    print("\n%s:  requests %d" % (lane, n))
    print("  clean %d (%.0f%%) · arity-miss %d (%.0f%%) · other %d (%.0f%%)"
          % (d["clean"], 100 * d["clean"] / n, d["arity_miss"], 100 * d["arity_miss"] / n,
             d["other"], 100 * d["other"] / n))
    if d["items_exp"]:
        print("  items across shape-ok envelopes: %d expected · dropped %d (%.1f%%) · extra %d · dup %d"
              % (d["items_exp"], d["items_drop"], 100 * d["items_drop"] / d["items_exp"], d["items_extra"], d["items_dup"]))
    print("  → NOT-USABLE (needs API/retry in production): %d/%d (%.0f%%)" % (n - usable, n, 100 * (n - usable) / n))
    for err, c in d["errs"].most_common():
        print("      %2d × %s" % (c, err))

print("\nbilled calls (MUST be 0 with refuse-billed): %d" % billed)
print("durable checkpoint: %s" % CKPT)
