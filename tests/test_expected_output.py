"""Expected OUTPUT tokens — measured, never read off `max_tokens`, and never 0.

THE COUPLING. The estimator read `max_tokens` as "expected output". That was always wrong — you are billed on
tokens GENERATED, never on the cap — but it stayed invisible while everyone set a cap. Then the correct fix for
a different problem (stop capping: a cap never controlled cost, and a low one destroys the call silently) made
it visible in the worst direction:

    WITH max_tokens=8000 : out_tok 8000   est $0.0400
    WITHOUT max_tokens   : out_tok    0   est $0.0000

Output is where the money is, so an uncapped 800-page job estimated $4.16 against a true $28.16. Under-estimating
is the sign that walks past a cap unchallenged.

ONE FIELD, THREE READERS: the API treats max_tokens as a hard ceiling, a pipeline treats it as truncation risk,
the estimator treated it as expected output. Decoupling is what ends that, rather than rotating whose turn it is
to be wrong.

THE INVARIANT, which recurred six times in one session: **absence is unknown — never zero, and never a
different field that happens to be nearby.** (unpriced ≠ $0 · empty ≠ no-findings · truncated ≠ clean ·
absent-cap ≠ free-output · context-window ≠ output-ceiling.)
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-expout-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import io, json, pathlib, contextlib
from spendguard import bulkgate, expected_output as eo, pricing, config

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


REAL, FAKE = "m-real-limits", "m-context-copied"
pathlib.Path(config.HOME).mkdir(parents=True, exist_ok=True)
_RATES = {"in_": 1.0, "out": 5.0, "cached_in": 0.1, "batch_in": 0.5, "batch_out": 2.5}
pathlib.Path(config.HOME, "litellm_prices.json").write_text(json.dumps(
    {"models": {REAL: _RATES, FAKE: _RATES},          # the BATCH estimators price their result, so both need rates
     "providers": {REAL: "test", FAKE: "test"}, "unit_models": {},
     # REAL publishes a distinct output ceiling; FAKE has the context window copied into it — which 961 of
     # 2,572 upstream entries actually do.
     "context": {REAL: {"max_input_tokens": 200_000, "max_output_tokens": 64_000},
                 FAKE: {"max_input_tokens": 8_191, "max_output_tokens": 8_191}}}))
pricing.CONTEXT_LIMITS = pricing._load_context()
pricing.PRICING = pricing._load()

print("-- the published output ceiling, and the context window wearing its name --")
check("a genuine output limit is returned", pricing.max_output_tokens(REAL) == 64_000)
check("it is NOT the context window", pricing.max_output_tokens(REAL) != pricing.max_input_tokens(REAL))
check("output == input is rejected as unpublished, not used as a limit",
      pricing.max_output_tokens(FAKE) is None, str(pricing.max_output_tokens(FAKE)))
check("…and the context window itself is still readable for what it IS",
      pricing.max_input_tokens(FAKE) == 8_191)

print("-- precedence: measured → caller's bound → published ceiling → UNKNOWN --")
# These assert the INVARIANTS, not one rung. The rung that fires depends on how much this machine has
# measured, and a test pinned to "the ceiling is the answer" is what kept a ceiling BEING the answer: a
# published limit is a BOUND, an estimate is an EXPECTATION, and using the first as the second over-states a
# real answer ~100x (opus/gpt-5.5 publish 128,000; their real answers here ran 400-2,100).
n, b = eo.expect(REAL, max_tokens=8_000)
check("a caller's cap is never exceeded — they cannot receive more than they allowed", n <= 8_000, f"{n} {b}")
check("...and the basis names which rung answered", b in ("learned", "model-history", "caller-cap"), f"{b}")
n, b = eo.expect(REAL)
check("no cap → never the CONTEXT WINDOW (a different field entirely)", n != pricing.max_input_tokens(REAL), f"{n}")
check("no cap → never ABOVE the published output ceiling", n <= pricing.max_output_tokens(REAL), f"{n} {b}")
check("no cap → a MEASUREMENT is preferred to the ceiling when one exists",
      (b == "model-max") == (not bulkgate.model_outputs(REAL).get("p90")), f"{b}")
n, b = eo.expect(FAKE)
check("no cap and no real ceiling → UNKNOWN", (n, b) == (0, "unknown"), f"{n} {b}")
check("…and 0 is returned WITH the unknown basis, never as a silent zero", b == "unknown")

print("-- the whole point: an absent cap is not free output --")
buf = io.StringIO()
# A MODEL NOBODY HAS WARNED ABOUT YET. expect() now emits the warning itself — that was the defect a
# 4-vendor review found and two validators confirmed — so reusing FAKE here would test a dedup slot that
# expect() had already consumed above, and read as "the warning never fires".
DEDUP_PROBE = FAKE + "-dedup-probe"
with contextlib.redirect_stderr(buf):
    eo.warn_unknown(DEDUP_PROBE)
    eo.warn_unknown(DEDUP_PROBE)
err = buf.getvalue()
# Assert the FACT and the countable behaviour, not the wording: expect() reports the basis structurally, and
# the dedup is a number. Whether a sentence reads well is not something a unit test can settle.
check("unknown output is announced at all", err.strip() != "")
# The fix itself: expect() must not return its silent zero silently.
buf2 = io.StringIO()
with contextlib.redirect_stderr(buf2):
    eo.expect(FAKE + "-expect-warns-probe")
check("expect() ITSELF announces the unknown, without a caller having to remember",
      buf2.getvalue().strip() != "", "warn_unknown existed and expect() never called it")
check("exactly once per model, not once per call", len(err.strip().splitlines()) == 1, str(err.count(chr(10))))
check("the basis itself says unknown — callers act on that, not on the prose",
      eo.expect(FAKE)[1] == "unknown")

print("-- learned measurement outranks every ceiling (and is bounded by a caller's cap) --")
from spendguard import bulkgate
SIG = "expout-sig"
for _ in range(eo.MIN_OBS + 5):
    bulkgate.note_response(SIG, REAL, 900, 64_000, "stop")       # complete outputs, ~900 tokens each
n, b = eo.expect(REAL, sig=SIG)
check("with history, the measurement wins over the 64k ceiling", b == "learned" and n < 64_000, f"{n} {b}")
check("…and it is a headroomed p99, not the bare median", n >= 900)
n2, _ = eo.expect(REAL, sig=SIG, max_tokens=500)
check("a caller's cap still bounds the learned figure (they cannot receive more)", n2 == 500, str(n2))

print("-- truncated samples must not drag the measurement down (censoring) --")
SIG2 = "expout-censored"
for _ in range(30):
    bulkgate.note_response(SIG2, REAL, 200, 200, "length")       # every one CUT at a 200 cap
mt = bulkgate.maxtokens(SIG2)
check("truncated calls are counted", mt["n_truncated"] == 30)
check("they are EXCLUDED from the percentile (they measure the cap, not the work)", mt["n"] == 0)
check("the recommendation is floored ABOVE the cap that truncated, never below",
      (mt["recommend"] or 0) >= 400, str(mt.get("recommend")))
check("and it says every sample was truncated", "TRUNCATED" in (mt.get("warn") or ""))

print("-- a truncation RATE is surfaced, not left for someone to go looking for --")
SIG3 = "expout-rate"
for _ in range(80):
    bulkgate.note_response(SIG3, REAL, 500, 1000, "stop")
for _ in range(20):
    bulkgate.note_response(SIG3, REAL, 1000, 1000, "length")
mt3 = bulkgate.maxtokens(SIG3)
check("the rate is exactly truncated/total", mt3["trunc_rate"] == 20 / 100.0, str(mt3.get("trunc_rate")))
check("a rate above zero always produces a warning to surface", bool(mt3.get("warn")))

print("-- the truncation warning dedups (327 identical lines is a warning nobody reads) --")
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    for _ in range(40):
        bulkgate.note_response("expout-noise", REAL, 200, 200, "length")
lines = [ln for ln in buf.getvalue().splitlines() if "TRUNCATED" in ln]
check("40 truncated calls do not produce 40 lines", len(lines) < 5, str(len(lines)))
check("but it does NOT go silent either — it re-announces as the count grows", len(lines) >= 2, str(len(lines)))
check("the line carries the RATE, not just one instance", "% of this class" in (lines[-1] if lines else ""))

print("-- every estimator path is decoupled from the cap, proved by what it RETURNS --")
# Not by grepping the source: a function can mention the helper and still fall back to the cap. Each estimator
# is called twice — once with a cap, once without — and the numbers say whether it is coupled.
from spendguard import gate, submit
MSG = [{"role": "user", "content": "extract the fields"}]
CASES = [
    ("gate._est_oai_chat", lambda cap: gate._est_oai_chat(
        dict({"model": REAL, "messages": MSG}, **({"max_tokens": cap} if cap else {})))[2]),
    ("gate._est_anth_msg", lambda cap: gate._est_anth_msg(
        dict({"model": REAL, "messages": MSG}, **({"max_tokens": cap} if cap else {})))[2]),
    ("gate._est_oai_resp", lambda cap: gate._est_oai_resp(
        dict({"model": REAL, "input": "extract"}, **({"max_output_tokens": cap} if cap else {})))[2]),
    ("gate._estimate_openai_jsonl", lambda cap: gate._estimate_openai_jsonl(json.dumps(
        {"body": dict({"model": REAL, "messages": MSG}, **({"max_tokens": cap} if cap else {}))}).encode())["out_tok"]),
    ("gate._estimate_anthropic_requests", lambda cap: gate._estimate_anthropic_requests(
        [{"params": dict({"model": REAL, "messages": MSG}, **({"max_tokens": cap} if cap else {}))}])["out_tok"]),
]
for name, call in CASES:
    with contextlib.redirect_stderr(io.StringIO()):
        capped_v, uncapped_v = call(8_000), call(None)
    check(f"{name}: an ABSENT cap does not mean zero output", uncapped_v > 0, str(uncapped_v))
    check(f"{name}: …and never exceeds the model's real ceiling", uncapped_v <= 64_000, str(uncapped_v))
    check(f"{name}: …and is never the CONTEXT WINDOW read as an output limit",
          uncapped_v != pricing.max_input_tokens(REAL), str(uncapped_v))
    check(f"{name}: a caller's cap is never exceeded", capped_v <= 8_000, str(capped_v))
    check(f"{name}: a capped request never estimates MORE than an uncapped one", capped_v <= uncapped_v,
          f"{capped_v} > {uncapped_v}")

with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
    fh.write(json.dumps({"body": {"model": REAL, "messages": MSG}}) + chr(10))
    _jsonl = fh.name
with contextlib.redirect_stderr(io.StringIO()):
    se = submit.estimate_jsonl_cost(_jsonl, REAL)
check("submit.estimate_jsonl_cost: an absent cap does not mean zero output", se["out_tok"] > 0,
      str(se["out_tok"]))
check("…and never above the model's published output ceiling", se["out_tok"] <= 64_000, str(se["out_tok"]))
check("…and it NAMES the rung it used, so a reader can tell a measurement from a ceiling",
      se.get("out_basis") in eo.BASES, str(se.get("out_basis")))
os.unlink(_jsonl)

print("-- an UNKNOWN output side is stated where the cap decision is read --")
check("unknown is called out as a FLOOR, not resolved to a number",
      "UNKNOWN" in gate._est_line({"cost": 1.0, "out_basis": "unknown"})
      and "FLOOR" in gate._est_line({"cost": 1.0, "out_basis": "unknown"}))
check("the model ceiling is named as a worst case, not as an expectation",
      "worst case" in gate._est_line({"cost": 1.0, "out_basis": "model-max"}))
check("a measured estimate says nothing extra (no noise on the normal path)",
      "UNKNOWN" not in gate._est_line({"cost": 1.0, "out_basis": "learned"}))

print("-- expected (p90) and cap-sizing (p99x1.5) are deliberately different numbers --")
mt4 = bulkgate.maxtokens(SIG)
check("both are reported", mt4.get("p90") and mt4.get("recommend"))
check("the cap-sizing figure is the larger of the two", mt4["recommend"] > mt4["p90"])
check("expect() uses the EXPECTED one, not the worst case", eo.expect(REAL, sig=SIG)[0] == int(mt4["p90"]),
      f"{eo.expect(REAL, sig=SIG)[0]} vs p90 {mt4['p90']}")

print("-- end to end: the number that started this --")
body = {"model": REAL, "messages": [{"role": "user", "content": "extract the fields"}]}
with contextlib.redirect_stderr(io.StringIO()):
    capped = gate._estimate_openai_jsonl(json.dumps({"body": dict(body, max_tokens=8000)}).encode())
    uncapped = gate._estimate_openai_jsonl(json.dumps({"body": body}).encode())
check("an uncapped request no longer estimates its output at ZERO", uncapped["out_tok"] > 0,
      str(uncapped["out_tok"]))
check("it stays within the model's real ceiling", uncapped["out_tok"] <= 64_000, str(uncapped["out_tok"]))
check("and the capped request is not estimated higher than the uncapped one",
      capped["out_tok"] <= uncapped["out_tok"], f"{capped['out_tok']} {uncapped['out_tok']}")
check("a capped one never exceeds the caller's bound", capped["out_tok"] <= 8_000, str(capped["out_tok"]))
check("the estimate says WHICH basis it used — on both the capped and uncapped paths",
      uncapped.get("out_basis") in eo.BASES and capped.get("out_basis") in eo.BASES,
      f"{uncapped.get('out_basis')} / {capped.get('out_basis')}")

print(f"\n{'[FAIL]' if failures else 'OK'} test_expected_output: {failures} failure(s)")
sys.exit(1 if failures else 0)
