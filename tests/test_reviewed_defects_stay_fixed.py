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
import os
import pathlib
import sys
import tempfile

# ISOLATE BEFORE IMPORTING ANYTHING. This file writes through spendguard's own paths, and on 2026-08-10 a
# check added here overwrote the user's real ~/.spendguard/config.json — 9KB of settings replaced by a
# 26-byte probe value, silently, with the suite still green. The package resolves its paths AT IMPORT, so
# the redirect has to happen before the first `from spendguard import ...` below, not after.
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reviewed-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

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


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE SPLITS. Sites where opus and gpt-5.5 DISAGREED, resolved by reading the real code — which is
# what a split needs: a third judgement grounded in the source, not a tie-break between two opinions.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
print("\n-- bool is a subclass of int, and the guard covered only half of it (output_contract) --")
from spendguard.output_contract import _check_schema as _cs2                 # noqa: E402
for _t in ("integer", "number"):
    try:
        _cs2(True, {"type": _t}, "$")
        check(f"a boolean is REJECTED where {_t} is required", False, "True passed as a number")
    except ValueError:
        check(f"a boolean is REJECTED where {_t} is required", True)
try:
    _cs2(True, {"type": "boolean"}, "$")
    check("...and a boolean is still accepted where boolean is required", True)
except ValueError:
    check("...and a boolean is still accepted where boolean is required", False)

print("\n-- a UTC timestamp with no offset is UTC, not local (ledger._day_period) --")
# fromisoformat returns a NAIVE datetime, and .astimezone() on a naive value assumes LOCAL time — so a
# UTC-canonical stamp was shifted by the host's offset and the charge landed on the wrong DAY, and at a
# month boundary in the wrong PERIOD.
from spendguard.ledger import _day_period as _dp                             # noqa: E402
check("a naive UTC stamp keeps its own day under a UTC period",
      _dp("2026-08-09T23:30:00", "UTC")[0] == "2026-08-09",
      str(_dp("2026-08-09T23:30:00", "UTC")))
check("...and an explicit offset is respected",
      _dp("2026-08-09T23:30:00+00:00", "UTC")[0] == "2026-08-09")

print("\n-- every Bedrock model call leaves a trace, even the ones we cannot meter --")
# Streaming ops carry their usage inside the response body, and reading it would consume what the caller
# is waiting for. "Cannot measure" was implemented as "do nothing", so a streaming call left NO row at
# all — ungoverned spend that the coverage number counts as covered.
from spendguard import bedrock_adapter as _ba                                # noqa: E402
check("streaming ops are recognised rather than ignored", bool(_ba._STREAM_OPS))
check("...and are not confused with the metered ones", not (_ba._OPS & _ba._STREAM_OPS))

print("\n-- a settings cache with no invalidation (config._cfg) --")
from spendguard import config as _cfg3                                       # noqa: E402
check("cfg_invalidate() exists for in-process writes", callable(_cfg3.cfg_invalidate))
# BEHAVIOUR, NOT AN ATTRIBUTE. The first version asserted hasattr(_cfg, "_mtime") — an implementation
# detail that is unset when no config file exists yet, so it failed in an isolated HOME while the caching
# worked correctly. What matters is that a rewritten file is SEEN.
# A TEST NEVER WRITES TO A REAL USER PATH. The first version of this check wrote to _cfg.CONFIG_JSON
# directly — and this file runs in the SHARED home, so it OVERWROTE ~/.spendguard/config.json with its own
# probe value and destroyed every setting in it. Measured: the file went from ~9KB to 26 bytes, calls
# logging silently switched off, and the next review wave's independent ledger cross-check read $0.00 as a
# result. A guard that damages the thing it guards is worse than no guard.
#
# CONFIG_JSON is redirected to a temp file for the duration, so the property is still exercised end to end
# and nothing outside the temp directory is touched.
import json as _json3, tempfile as _tf3, time as _time3, pathlib as _pl3     # noqa: E402
_real_cfg_path = _cfg3.CONFIG_JSON
_tmp_cfg = _pl3.Path(_tf3.mkdtemp(prefix="spendguard-cfgtest-")) / "config.json"
_cfg3.CONFIG_JSON = _tmp_cfg
try:
    _tmp_cfg.write_text(_json3.dumps({"probe": {"k": "first"}}))
    _cfg3.cfg_invalidate()
    check("a config value reads back", _cfg3._cfg_get("probe", "k") == "first",
          str(_cfg3._cfg_get("probe", "k")))
    _time3.sleep(0.01)                                                       # distinct mtime
    _tmp_cfg.write_text(_json3.dumps({"probe": {"k": "second"}}))
    check("...and a REWRITE of the file is seen without an explicit invalidate",
          _cfg3._cfg_get("probe", "k") == "second",
          f"got {_cfg3._cfg_get('probe', 'k')!r} — a cache with no invalidation freezes a setting at "
          f"whatever time it was first needed")
finally:
    _cfg3.CONFIG_JSON = _real_cfg_path
    _cfg3.cfg_invalidate()
check("...and the REAL config path was never written to",
      _cfg3.CONFIG_JSON == _real_cfg_path and _tmp_cfg.exists())


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WAVE 3. 10 files, first clean 4-vendor panel, 22 confirmed.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
print("\n-- a $0 alert threshold means MORE sensitive, not less (report) --")
# `(a.alert_threshold or 1e9) * 0.1` collapsed a deliberate 0 into max(1.0, 0.0) — so the user asking for
# maximum sensitivity got the leak alert on everything over a dollar. The two readings of 0 point in
# exactly opposite directions, which is the worst case for a silent coercion.
import inspect as _i4                                                        # noqa: E402
from spendguard import report as _rep                                        # noqa: E402
check("the threshold is read with `is not None`, not `or`",
      "alert_threshold is not None" in _i4.getsource(_rep),
      "`or 1e9` turns an explicit 0 into the least sensitive setting there is")

print("\n-- an UNDATED record is not an OLD one (sources) --")
# `("" < cutoff)` is True for every non-empty cutoff, so any entry with a missing day was dropped from the
# window — spend excluded from a total for having no date.
from spendguard import sources as _src                                       # noqa: E402
check("the cutoff comparison requires a day to compare",
      "_day and _day < cutoff" in _i4.getsource(_src))

print("\n-- INSERT OR REPLACE cannot replace on a random primary key (semcache) --")
# Every put() therefore INSERTED, the cache grew without bound, and get()'s "LIMIT 1 with no ORDER BY"
# could keep serving the OLD output for a re-cached prompt forever.
from spendguard import semcache as _sc                                       # noqa: E402
check("put() deletes the natural key before inserting",
      "DELETE FROM semcache WHERE model=? AND prompt_hash=?" in _i4.getsource(_sc.put))

print("\n-- a confidence that is null or non-numeric is UNSTATED, not a crash (review) --")
from spendguard import review as _rev                                        # noqa: E402
for _v, _want in ((None, 0.5), ("high", 0.5), ("", 0.5), (0.9, 0.9), (2.0, 1.0), (-1, 0.0)):
    check(f"_conf({_v!r}) -> {_want}", _rev._conf(_v) == _want, str(_rev._conf(_v)))

print("\n-- SQL column names are whitelisted, not interpolated (learn) --")
from spendguard import learn as _lrn                                         # noqa: E402
try:
    _lrn.update_insight("x", **{"confidence=1; DROP TABLE insights--": 1})
    check("a non-column key is REFUSED", False, "it was accepted into the SET clause")
except ValueError:
    check("a non-column key is REFUSED", True)


# ── cross-file DRIFT found by the capability axis (2026-08-10) ────────────────────────────────────────────
# These read SOURCE rather than behaviour in a few places: the defect is often an absent guard, and the
# shortest honest proof that a guard is present is that its line is there. Each such check is paired with a
# behavioural one where the behaviour is reachable offline.
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "spendguard"
# Each of these was a copy of a job that another module already did correctly. The copy is what regressed,
# so the guard pins the copy's behaviour to the canonical one's.

# chat.day_totals leaked across orgs: `and r["org"]` made the filter run only for rows that HAD an org, so an
# unclassified conversation skipped it entirely and pushed under whatever org was connected.
_chat_src = (SRC / "chat.py").read_text()
check("chat.day_totals does not skip the org filter for unclassified rows",
      'if org_label and (r.get("org") or "").lower() != org_label.lower():' in _chat_src
      and 'and r["org"] and' not in _chat_src)

# chat.sync POSTed without a replace directive, so the server STACKED rows on every re-classification.
check("chat.sync declares a replace so server rows are pruned, not accumulated",
      '"replace": [{"channel": "claude-ai", "billed": False}]' in _chat_src)

# `r.get('cost', 0)` defaults only on a MISSING key; an unpriced model puts a present None there, which
# reaches :.4f and raises — after the call was billed.
check("chat.story survives a present-but-None cost", "r.get('cost', 0):.4f" not in _chat_src)

# One quantile algorithm. _pctl returned 0 on an empty sample — a confident wrong answer that max_tokens
# would then be sized from — and _sec used a different, off-by-one nearest-rank index.
from spendguard import bulkgate, calibrate                                             # noqa: E402
check("_pctl says UNKNOWN on an empty sample, never 0", bulkgate._pctl([], 0.9) is None)
check("_pctl and calibrate._quantile agree", bulkgate._pctl([1, 2, 3, 4], 0.5) == int(calibrate._quantile([1, 2, 3, 4], 0.5)))

# codex_exec._bin consulted PATH BEFORE the env pin, silently ignoring an explicit $SPENDGUARD_CODEX_BIN.
check("codex_exec._bin has no shutil.which fast path ahead of the pin",
      'if shutil.which("codex")' not in (SRC / "codex_exec.py").read_text())

# modal_adapter.account_total returned None for an empty window, conflating a real $0 with an unreadable bill.
check("modal account_total distinguishes an unreadable bill from a $0 window",
      "a successful read of an empty window IS $0.00" in (SRC / "modal_adapter.py").read_text())

# THE SEAM: the gate wrote gate_ledger, the calibrator read cost_predictions, nothing bridged them — so
# spendguard's own estimates never reached spendguard's own learned estimator.
check("bulkgate.record_estimate forwards the prediction to the calibrator",
      "calibrate.record_prediction(" in (SRC / "bulkgate.py").read_text())
check("the record_estimate name collision is resolved", hasattr(calibrate, "record_prediction"))

# "Is this a genuine human ask?" was decided by three substrings, in TWO places. A real ask that opened with
# "the tool_result came back empty" was silently DROPPED, taking its whole segment with it.
from spendguard import conv                                                            # noqa: E402
for _t in ("the tool_result came back empty, can you check?", "<thinking> is what I want explained",
              "[Request interrupted] — please resume"):
       check(f"a real ask beginning {_t[:24]!r} is not dropped", bool(conv._is_user_ask({"type": "user"}, _t)))
check("the ask decider is agentic and lives in ONE place", callable(getattr(conv, "classify_user_asks", None)))
check("claudecode does not keep its own copy of the ask rules",
      '"tool_result" not in t[:40]' not in (SRC / "claudecode.py").read_text())

# claudecode.work could not tell an unreadable session directory from a period with no work.
check("claudecode.work distinguishes an unreadable dir from no work",
      "nothing could be READ, which is not" in (SRC / "claudecode.py").read_text())


# An unpriced model took the WHOLE OpenAI report down with a KeyError, while the Anthropic path beside it
# had degraded gracefully since day one. Unpriced is never silently $0 — it contributes 0 and is NAMED.
from spendguard import pricing as _pr, reconcile_anthropic as _ra                      # noqa: E402
_pr.UNPRICED_SEEN.clear()
check("an unpriced model costs 0 without raising", _pr.cost_or_unpriced("no-such-model-zzz", 1000, 10) == 0.0)
check("...and the model is NAMED, not silently absorbed", "no-such-model-zzz" in _pr.UNPRICED_SEEN)
check("there is ONE unpriced registry, not one per module", _ra.UNKNOWN_MODELS is _pr.UNPRICED_SEEN)
_pr.UNPRICED_SEEN.clear()

# reconcile_anthropic._get passed NO timeout and handed back an unread, unclosed response — a provider stall
# became a hung reconcile with no error and no output.
check("no module opens its own socket to a provider any more",
      "urlopen" not in (SRC / "reconcile_anthropic.py").read_text())
check("the shared transport always passes a timeout",
      "timeout=timeout" in (SRC / "config.py").read_text())

# cachetest had a second, weaker system-prompt extractor whose regex truncated at an escaped quote — and the
# truncated length feeds the "is this worth caching" threshold.
check("cachetest uses the AST extractor, not a second regex scan",
      "_sys_assignments_ast" in (SRC / "cachetest.py").read_text()
      and "_SYS_ASSIGN" not in (SRC / "cachetest.py").read_text())


# ── silent failures that SURVIVED adversarial refutation (2026-08-11) ─────────────────────────────────────
# 179 swallowing functions triaged agentically; 60 called dangerous; 26 kept after refuters tried to knock
# them down; 7 of those confirmed by reading. These are the ones where the swallow LOSES MONEY OR TRUTH.

# gate._rt_flush emptied the aggregate BEFORE writing, so a failed write deleted real spend records for
# good — in the tool whose whole purpose is not losing track of spend.
_gsrc = (SRC / "gate.py").read_text()
check("a failed realtime-log write HOLDS the rows for the next flush instead of dropping them",
      "HELD for the next flush, not dropped" in _gsrc)
from spendguard import gate                                                            # noqa: E402
gate._rt_agg.clear()
gate._rt_agg[("2026-01-01", "openai", "m")] = [1, 0.5, 10, 5, 0]
_orig_log = gate.RT_LOG
gate.RT_LOG = "/nonexistent-dir-for-this-test/rt.jsonl"
try:
    gate._rt_flush()
    check("...proven: the row is still in the aggregate after an unwritable log", len(gate._rt_agg) == 1)
    check("...with its values intact", gate._rt_agg.get(("2026-01-01", "openai", "m"), [0])[1] == 0.5)
finally:
    gate.RT_LOG = _orig_log
    gate._rt_agg.clear()

# _tool_fee_count returned the PARTIAL count on a parse failure — money the gate silently did not charge,
# surfacing later only as an unexplained reconcile residual.
class _BadResp:
    @property
    def output(self):
        raise RuntimeError("unparseable")
check("an uncountable tool-fee response is UNKNOWN, not a partial number", gate._tool_fee_count(_BadResp()) is None)


class _CleanResp:
    output = []
    usage = None
check("...and a genuinely fee-free response is still 0, not None", gate._tool_fee_count(_CleanResp()) == 0)
check("the caller keeps UNKNOWN and zero apart too", "if n is None:" in _gsrc and "if n == 0:" in _gsrc)

# Two copies of "meta spend today" returned 0.0 when the ledger could not be read, and both figures are
# shown to an operator deciding whether to approve a run.
for _m, _f in (("advisor", "_meta_spent"), ("experiment", "_meta")):
    check(f"{_m}.{_f} says UNKNOWN rather than $0 when the ledger is unreadable",
          "return 0.0" not in (SRC / f"{_m}.py").read_text().split(f"def {_f}(")[1].split("def ")[0])

# calibrate.estimate collided with pricing.estimate, and pricing's is what `spendguard.estimate` binds — so
# the plain name reached the NAIVE figure while the learned predictor had to be asked for by module path.
from spendguard import calibrate as _cal                                               # noqa: E402
check("the learned predictor has a name that says so", callable(getattr(_cal, "predict_cost", None)))
check("...with the old name kept as an alias for existing consumers", _cal.estimate is _cal.predict_cost)


print(f"\n{'[FAIL]' if failures else 'OK'} test_reviewed_defects_stay_fixed: {failures} failure(s)")
sys.exit(1 if failures else 0)
