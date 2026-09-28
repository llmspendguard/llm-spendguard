"""BEHAVIORAL guards for the 🟠 lower-severity caller-intent fixes (2026-09-28). Each executes the fixed path:

  P5 edge values:
    · resource_state._num('nan'|'inf') → default (NaN/inf pass float() but never expire a cooldown → treat as malformed)
    · conv._dedup_top(events, 0) → [] (k<=0 means none; the loop appended one before the len>=k check)
    · advisor._judge_sample(per, limit=0) → [] (limit=0 asks for zero samples)
  P6 JSON-shape (a valid-but-non-object file must read as empty, not AttributeError/TypeError on .items()/led[k]=v):
    · workdone.load_summaries · verdict_ledger.load_verdicts · resources._load_history — all return {} for a non-dict file

Isolation: SPENDGUARD_HOME → mkdtemp before importing spendguard.
"""
import os, sys, json, tempfile, pathlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-amber-")

from spendguard import resource_state, conv, advisor, workdone, verdict_ledger, resources   # noqa: E402


class Checks:
    def __init__(self): self.fails = 0
    def ck(self, name, cond, extra=""):
        if not cond: self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra and not cond else ''}")


def main():
    chk = Checks()

    # ── P5: NaN/inf are malformed (they break every `<= now` comparison → a cooldown that never expires) ──
    chk.ck("_num('nan') → default (not NaN)", resource_state._num("nan") == 0.0)
    chk.ck("_num('inf') → default (not inf)", resource_state._num("inf") == 0.0)
    chk.ck("_num('-inf') → default", resource_state._num("-inf") == 0.0)
    chk.ck("_num('2.5') → 2.5 (a real value is untouched)", resource_state._num("2.5") == 2.5)

    # ── P5: k<=0 / limit<=0 mean NONE, not one ──
    evs = [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}]
    chk.ck("_dedup_top(events, 0) → [] (not 1 item)", conv._dedup_top(evs, 0) == [])
    chk.ck("_dedup_top(events, 2) still returns 2", len(conv._dedup_top(evs, 2)) == 2)
    rows = [("io1", "intentA", "modelA", "p", "o"), ("io2", "intentA", "modelA", "p", "o")]
    chk.ck("_judge_sample(per, limit=0) → [] (zero samples)", advisor._judge_sample(5, limit=0, rows=rows) == [])
    chk.ck("_judge_sample(per, limit=None) still samples", len(advisor._judge_sample(5, limit=None, rows=rows)) >= 1)

    # ── P6: a valid-but-non-object file reads as {} (callers do .items() / led[k]=v) ──
    def _write(p, obj):
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(p).write_text(json.dumps(obj))

    _sum = workdone._summaries_path()
    _write(_sum, ["not", "a", "dict"])                       # a JSON LIST where a map is expected
    chk.ck("workdone.load_summaries → {} for a non-dict file", workdone.load_summaries() == {})
    _write(_sum, {"proj": "did stuff"})
    chk.ck("workdone.load_summaries → the dict when it IS a dict", workdone.load_summaries() == {"proj": "did stuff"})

    _hist = resources._history_path()
    _write(_hist, [1, 2, 3])
    chk.ck("resources._load_history → {} for a non-dict file", resources._load_history() == {})

    _repo = tempfile.mkdtemp()
    _vp = verdict_ledger.verdict_path(_repo, "amber-ledger")
    _write(_vp, "a bare string")                             # valid JSON scalar → not a verdict map
    chk.ck("verdict_ledger.load_verdicts → {} for a non-dict file (led[k]=v would else crash)",
           verdict_ledger.load_verdicts(_repo, "amber-ledger") == {})

    print(f"\n{'[FAIL]' if chk.fails else '[OK]'} test_amber_edge_and_shape_fixes: {chk.fails} failure(s)")
    return 1 if chk.fails else 0


if __name__ == "__main__":
    sys.exit(main())
