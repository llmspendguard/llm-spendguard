"""Defects found by a 4-vendor review and confirmed by two independent validators, each with a guard.

WHY THIS FILE EXISTS. A fix without a test is a fix that comes back — that is how `_MARKER_MODELS` sat
defined-and-unreferenced and how output_cap's measured rung could never fire. Every entry below was:
found by 2+ vendors independently, then confirmed by BOTH opus and gpt-5.5 reading the real source. The
gate was itself checked with a negative control: a fabricated defect against code that explicitly guards
the case was rejected by both validators, each citing the guard by line.

The pattern in the first two is worth naming: BOTH are the project's core invariant violated inside the
code written to enforce it. That is not irony, it is the reason external review is worth paying for — the
author is the last person able to see it.
"""
import contextlib
import io
import sys

from spendguard import expected_output as eo, reconcile

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_expected_output_never_returns_a_SILENT_zero():
    """expected_output.py:79 — 2 vendors, both validators, HIGH.

    warn_unknown() existed, said "an estimate that silently treats unknown output as ZERO is the bug this
    module exists to end", and expect() returned the zero WITHOUT EVER CALLING IT. The module that forbids
    silent zeros was producing one."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        n, basis = eo.expect("a-model-that-cannot-possibly-be-priced-xyz")
    check("the unknown path still returns a number a caller can do arithmetic with", n == 0)
    check("...and NAMES itself unknown", basis == "unknown", basis)
    check("...and is NOT silent about it", bool(err.getvalue().strip()),
          "returning 0 quietly is the exact failure this module was written to prevent")


def test_a_measured_provider_total_of_zero_is_not_treated_as_absence():
    """reconcile.py:63 — 2 vendors, both validators.

    `if not truth_total: return None` swallowed 0.0 four lines below a docstring promising that a failed
    fetch never reads as "$0 / 100% covered". A provider that billed NOTHING while we attribute spend to it
    is the loudest leak there is, and it was the quietest."""
    msg = reconcile.residual_warning(0.0, 12.50)
    check("truth=$0 with $12.50 attributed WARNS", bool(msg), repr(msg))
    check("...and says the number is not reconciled", msg and "reconcile" in msg.lower(), str(msg)[:80])
    check("truth=$0 with $0 attributed stays quiet — nothing is wrong there",
          reconcile.residual_warning(0.0, 0.0) is None)
    check("truth=None (fetch FAILED) is still distinct from truth=0 (provider billed nothing)",
          "UNKNOWN" in (reconcile.residual_warning(None, 12.50) or ""),
          "an unreadable bill and a zero bill are different facts and must not share a message")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WAVE 1, the singletons. Each was confirmed by both validators reading the real source.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
from spendguard import cascade, callio, close, output_contract          # noqa: E402

print("\n-- nothing tried is not something tried (cascade) --")
# An empty ladder skipped the loop and returned n_tried=1 with output='' — shaped exactly like a model
# answering with an empty string, which is a completely different event.
_r = cascade.cascade("hi", [], intent=None, _caller=lambda m, p: (0.0, "x"))
check("an empty ladder reports n_tried=0, not 1", _r["n_tried"] == 0)
check("...and output is None, not '' (which reads as an empty answer)", _r["output"] is None)
check("...and it says WHY rather than returning a bare shape", bool(_r.get("why")))

# A rung that RAISES is a rung that failed, and escalating past a failure is what a cascade is for.
_calls = []
def _flaky(m, p):
    _calls.append(m)
    if m == "cheap":
        raise TimeoutError("cheap model timed out")
    return (0.01, "answer from " + m)
_r = cascade.cascade("hi", ["cheap", "strong"], verify=lambda p, o: True, _caller=_flaky)
check("a rung that raises escalates instead of killing the run", _r["model"] == "strong", str(_r))
check("...and the failure is RECORDED, not swallowed", bool(_r.get("errors")), str(_r.get("errors")))
check("...and both rungs were actually attempted", _calls == ["cheap", "strong"], str(_calls))

print("\n-- limit=0 means no rows, not no limit (callio) --")
# unjudged(0) returned EVERY unjudged row. The caller most likely to pass 0 is a budget loop that has run
# out of room — the worst possible moment to hand back the whole table.
check("unjudged(0) returns nothing", callio.unjudged(0) == [])

print("\n-- a callable's identity includes its module (output_contract) --")
# Two modules each defining `validate` produced the same identity, so one contract's test flag satisfied
# the other's — the exact staleness this identity exists to expire.
def validate(x):        # noqa: E306
    return True
_a = output_contract.describe(validate)
validate.__module__ = "some.other.module"
_b = output_contract.describe(validate)
check("same qualname in different modules -> different identities", _a != _b, f"{_a} vs {_b}")

print("\n-- a month string is parsed, not eyeballed (close) --")
# `len==7 and [4]=="-"` accepted '2024-99', which built a window no row can fall in and closed the month
# at $0.00 without complaint.
for _bad in ("2024-ab", "2024-99", "2024-00"):
    _out = io.StringIO()
    with contextlib.redirect_stdout(_out):
        _rc = close.main(["--month", _bad])
    check(f"--month {_bad} is refused with a usage message", _rc == 2 and "YYYY-MM" in _out.getvalue())





# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WAVE 2, after agentic near-miss grouping. Exact-line matching found 5 candidate sites and 1 real
# defect; grouping findings that describe ONE defect at slightly different lines found 33 and 10.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
print("\n-- a variant with NO measurement is killed, not promoted (experiment) --")
# _measure swallowed every failure, and the caller read `killed = scores and mean(scores) < THRESH`.
# scores=[] is falsy, so a variant whose pilot calls ALL failed read as NOT killed — and was expanded to
# the full sample and paid for. The one arm proven unable to answer got the most money.
import inspect as _i                                                         # noqa: E402
from spendguard import experiment as _x                                      # noqa: E402
_src = _i.getsource(_x.run) if hasattr(_x, "run") else _i.getsource(_x)
check("an empty score list is handled BEFORE the threshold comparison",
      "if not scores:" in _src and "no successful pilot call" in _src,
      "`scores and mean < THRESH` treats 'no measurement' as 'passed'")
check("...and failures are counted rather than swallowed", "fails.append" in _i.getsource(_x))

print("\n-- a failed bulk load is rolled back, not left for the next commit (ledger.bulk) --")
from spendguard.ledger import SpendLedger as _SL                             # noqa: E402
check("bulk() rolls back on exception", "rollback()" in _i.getsource(_SL.bulk),
      "deferred commits meant a raised bulk load sat in an OPEN transaction and landed later, silently")

print("\n-- and one the code was RIGHT about (clear_true_down) --")
# Both validators confirmed that clearing a failed-fetch provider's corrections was a bug. It is not:
# test_true_down.py predates the finding and encodes the intent — a provider we could not read is NEVER
# trued down, so its rows fall back to the gate ESTIMATE, which is labelled as an estimate. Keeping the
# old correction would leave a stale number wearing the stronger 'billed truth' label.
from spendguard import budget as _b                                          # noqa: E402
check("clear_true_down still clears unconditionally, and says why",
      "providers" not in _i.signature(_b.clear_true_down).parameters
      and "more honest" in (_b.clear_true_down.__doc__ or ""),
      "2-of-2 validator agreement is evidence, not proof — the second false positive today")


print("\n-- wave 2, the rest (7 confirmed after grouping) --")
from spendguard import config as _cfg, config_schema as _cs, setup as _st                # noqa: E402

# OWNERSHIP OF AN INTERPRETER HOOK IS A MARKER WE WROTE, not the word "spendguard" appearing anywhere.
# `"spendguard" in contents` is satisfied by a user's own sitecustomize that merely mentions us in a
# comment — and --uninstall then DELETED it. That is not recoverable from here.
check("the gate hook carries a distinctive ownership marker", _st._SENTINEL in _st._HOOK)
check("...and a file that only MENTIONS spendguard is not treated as ours",
      _st._SENTINEL not in "# my own hook; mentions spendguard in a comment\nimport os")

# A glob hit can vanish before the stat() — these directories include version-manager trees that rewrite
# themselves. An unguarded stat() raised out of a function whose job is "find it, or return None".
check("a CLI candidate that disappears mid-search does not raise",
      _cfg.resolve_cli("a-binary-that-does-not-exist-anywhere") is None)

# Two declared kinds were not in this file's own vocabulary — "number" where everything else uses
# int/float, and "float" on a character count.
_kinds = {e.get("kind") for e in _cs.SETTINGS}
check("no one-off 'number' kind survives", "number" not in _kinds, str(sorted(_kinds))[:120])
check("snippet_len is an int, not a float", next(
    e for e in _cs.SETTINGS if e["key"] == "snippet_len")["kind"] == "int")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WAVE 1, RE-GROUPED. 53 sites -> 110, and 57 of them were invisible to exact-line matching.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
print("\n-- the receipt honours its own scope (receipt.tally) --")
# tally() took `project` and `conv` and used NEITHER, while budget.spent_since supported both all along.
# So tally(project=repo) — which is how the per-repo receipt is built — returned the GLOBAL total under a
# per-repo heading. The number the receipt exists to make honest was the one number in it that was not.
from spendguard import receipt as _r                                         # noqa: E402
_glob = _r.tally()["api"]["month"]
_none = _r.tally(project="a-repo-that-has-never-spent-anything")["api"]["month"]
check("a repo with no spend reports 0, not the global total", _none == 0 or _none < _glob,
      f"scoped {_none} vs global {_glob}")

print("\n-- an unreadable price file is never overwritten (pricing.set_price) --")
# `except: data = {}` + a full rewrite DESTROYED every verified price on one bad read, in the file whose
# whole discipline is "prices enter this table only with provenance".
import json as _json, os as _os2, tempfile as _tf2                           # noqa: E402
from spendguard import pricing as _p2                                        # noqa: E402
_saved = _os2.environ.get("SPENDGUARD_HOME")
_os2.environ["SPENDGUARD_HOME"] = _tf2.mkdtemp(prefix="spendguard-prices-")
try:
    _path = _p2.user_prices_path()
    _os2.makedirs(_os2.path.dirname(_path), exist_ok=True)
    with open(_path, "w") as _fh:
        _fh.write('{"providers": {"acme": {"m1": {"in_": 1.0}}}, BROKEN')
    try:
        _p2.set_price("m2", "acme", 2.0, 4.0, source="test")
        check("an unreadable prices.json is REFUSED, not replaced", False, "it overwrote the file")
    except ValueError:
        check("an unreadable prices.json is REFUSED, not replaced", True)
    with open(_path) as _fh:
        check("...and the original content survives", "m1" in _fh.read())
finally:
    if _saved is None:
        _os2.environ.pop("SPENDGUARD_HOME", None)
    else:
        _os2.environ["SPENDGUARD_HOME"] = _saved

print("\n-- a call class is the WHOLE prompt, not its first 512 chars (bulkgate.sig) --")
# A shared preamble longer than 512 chars is the norm here — system blocks, review briefs, compacted
# source. Different calls collided into one class, so caps, latencies and cache entries from one were
# served to another.
from spendguard import bulkgate as _bg                                       # noqa: E402
check("two prompts sharing a 512-char preamble get different signatures",
      _bg.sig("m", prompt="X" * 512 + "AAAA") != _bg.sig("m", prompt="X" * 512 + "BBBB"))

print("\n-- a callable's identity is its CODE, not its name (output_contract) --")
# module+qualname was this morning's fix, and every module-level lambda is `<lambda>` in the same module,
# so they all still shared one identity. Found only by re-validating the FIXED code.
from spendguard import output_contract as _oc2                               # noqa: E402
_l1, _l2 = (lambda x: x + 1), (lambda x: x + 2)
_l1.__module__ = _l2.__module__ = "same.module"
check("two different lambdas in one module get different identities",
      _oc2.describe(_l1) != _oc2.describe(_l2), f"{_oc2.describe(_l1)} vs {_oc2.describe(_l2)}")
print(f"\n{'[FAIL]' if failures else 'OK'} test_reviewed_defects_stay_fixed: {failures} failure(s)")
sys.exit(1 if failures else 0)
