"""call_io LIVE capture — the path that unblocks measuring realtime/lane-only intents (roadmap B).

call_io used to be populated ONLY by BATCH recovery (fetch-io downloads OpenAI/Anthropic batch input/output files).
An estate whose work runs realtime/lane (honestreview refute, 7thsense comprehend, warden describe) never becomes a
provider batch, so fetch-io had nothing to recover and those intents had ZERO replay bodies — bakeoff / effort-titrate
refused them. `callio.capture_live` records a workload call's prompt+output into the corpus AS IT RUNS, and
`adapters.call` calls it after every served workload call. This guards the contract:

  · OPT-IN (default off; full bodies are privacy-sensitive) — no capture unless capture_live_on().
  · WORKLOAD only — spendguard:* meta / untagged '(none)' calls are skipped.
  · BOUND BY CONTAINMENT, NOT A CUT — an oversized prompt is SKIPPED (never stored as a replay-poisoning partial);
    a captured prompt is WHOLE → truncated=0 → REPLAYABLE, so bakeoff._sample_prompts can read it.
  · BOUNDED per (intent, model) at the cap.
  · adapters.call feeds it end-to-end (sig=intent → a captured row → sample-able).

Offline + hermetic: temp HOME, provider stubbed, no network, no LLM.
"""
import os
import sys
import tempfile

import atexit as _atexit   # noqa: E402
import shutil as _shutil   # noqa: E402
_SG_HOME = tempfile.mkdtemp(prefix="sg-live-")
os.environ["SPENDGUARD_HOME"] = _SG_HOME
_atexit.register(_shutil.rmtree, _SG_HOME, ignore_errors=True)   # clean up the temp HOME — don't leak it into $TMPDIR
os.environ["SPENDGUARD_CAPTURE_LIVE"] = "1"               # opt in (default is off)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import callio, bakeoff, adapters   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []

print("-- capture_live is OPT-IN: a no-op when disabled --")
os.environ["SPENDGUARD_CAPTURE_LIVE"] = "0"
_off = callio.capture_live("wl-intent", "openai", "gpt-x", "a real task prompt", "an answer")
fails += report_check("disabled → no capture (returns None)", _off is None and callio.count_rows("wl-intent", "gpt-x") == 0)
os.environ["SPENDGUARD_CAPTURE_LIVE"] = "1"

print("\n-- enabled: a WORKLOAD call is captured WHOLE (replayable, truncated=0) --")
rid = callio.capture_live("code-review", "openai", "gpt-x", "review def foo(): return 1/0", "found a div-by-zero",
                          in_tok=8, out_tok=4)
fails += report_check("a workload call is captured", bool(rid) and callio.count_rows("code-review", "gpt-x") == 1)
_row = callio._callio_db().execute("SELECT prompt, output, source, truncated FROM call_io WHERE intent='code-review'").fetchone()
fails += report_check("the prompt is stored WHOLE (== the real prompt, not a snip)", _row and _row[0] == "review def foo(): return 1/0")
fails += report_check("truncated=0 → REPLAYABLE (source tags it as live_io)", _row and _row[3] == 0 and _row[2] == "live_io")

print("\n-- the captured task is SAMPLE-ABLE by the sweep (bakeoff._sample_prompts reads it) — the B unblock --")
_samples = bakeoff._sample_prompts("code-review", 5)
fails += report_check("bakeoff._sample_prompts returns the captured prompt (effort-titrate/bakeoff can now run it)",
                      _samples == ["review def foo(): return 1/0"])

print("\n-- spendguard:* META intents and untagged calls are SKIPPED (the corpus is the user's real work) --")
_meta = callio.capture_live("spendguard:effort-probe", "openai", "gpt-x", "Reply: OK", "OK")
_none = callio.capture_live("", "openai", "gpt-x", "some prompt", "out")
fails += report_check("a spendguard:* meta intent is skipped", _meta is None and callio.count_rows("spendguard:effort-probe", "gpt-x") == 0)
fails += report_check("an untagged '(none)' call is skipped", _none is None)

print("\n-- BOUND BY CONTAINMENT: an oversized prompt is SKIPPED, never stored as a partial --")
_orig_snip = callio.live_snip_chars
callio.live_snip_chars = lambda: 500                      # shrink the whole-fidelity bound for the test
try:
    _big = callio.capture_live("big-intent", "openai", "gpt-x", "P" * 600, "out")   # 600 > 500 → skip
    _fits = callio.capture_live("big-intent", "openai", "gpt-x", "Q" * 400, "out")   # 400 <= 500 → whole
finally:
    callio.live_snip_chars = _orig_snip
fails += report_check("a prompt larger than the fidelity bound is SKIPPED (not stored truncated)",
                      _big is None and callio.count_rows("big-intent", "gpt-x") == 1)   # only the fitting one landed
fails += report_check("a fitting prompt is stored WHOLE (truncated=0)",
                      bool(_fits) and bakeoff._sample_prompts("big-intent", 5) == ["Q" * 400])

print("\n-- adapters.call feeds capture end-to-end: sig=intent → a served workload call → a sample-able row --")
_orig_guarded = adapters._call_guarded


def _stub_guarded(*a, **kw):
    return {"text": "the model answer", "provider": "openai", "model": "gpt-x", "in_tok": 5, "out_tok": 3,
            "cost": 0.01, "executor": "api", "error": None}


adapters._call_guarded = _stub_guarded
try:
    adapters.call("gpt-5.6-luna", "classify this ticket: refund please", sig="ticket-triage")
finally:
    adapters._call_guarded = _orig_guarded
fails += report_check("adapters.call(sig=X) on a served call captured a call_io row for X",
                      callio.count_rows("ticket-triage", "gpt-x") == 1)
fails += report_check("...and it is the real prompt, sample-able for the sweep",
                      bakeoff._sample_prompts("ticket-triage", 5) == ["classify this ticket: refund please"])

print(f"\n{'[FAIL]' if fails else 'OK'} test_callio_live_capture: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
