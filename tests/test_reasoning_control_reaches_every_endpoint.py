"""Reasoning is billed as OUTPUT, and output was 91% of the bill. Every endpoint gets the control.

WHY THIS GUARD EXISTS. Measured over a four-vendor code review: 91% of the cost was output, and 92-98% of
that output was reasoning nobody ever sees. glm-5.2 emitted 11,176 tokens per call to deliver 262 tokens of
findings. The prompt was not the problem — the findings were terse, 4-7 per file, exactly as asked.

The problem was one line:

    if re.match(r"(gpt-5|o[134])", raw, re.I):
        okw["reasoning_effort"] = reasoning or "minimal"

A hardcoded list of OpenAI's own model names, so the cost control reached the models that needed it LEAST
and never reached kimi-k3 or glm-5.2, which reason the most. Both accept the parameter. Measured directly:
minimal cut kimi-k3 from 316 output tokens to 92 and glm-5.2 from 898 to 60; on a real review file,
8,008 -> 570 and 11,176 -> 116.

BUT THE FIRST FIX WAS ALSO WRONG, and the quality measurement is why. Replacing the regex with
`reasoning or "minimal"` swapped one hand-picked bound for another, and MEASURED it destroys the work:

    calls.py   glm-5.2  minimal ->    10 output tokens,  0 findings
    calls.py   glm-5.2  high    ->   793 output tokens,  1 finding (the real naive-timestamp bug)
    pricing.py kimi-k3  minimal ->  1,522 tokens, 3 findings   <- MORE than high, so not even monotonic
    pricing.py kimi-k3  high    ->  3,848 tokens, 2 findings

The "96x cheaper" was 96x cheaper because it had stopped reviewing. Effort is a property of
(call-class, model), settled by measurement — and until a class HAS one, nothing is sent and the vendor's
own default applies. An invented bound is worse than no bound: no bound is at least honest.

HOW THIS FILE ASSERTS ALL THAT — AND WHY IT CHANGED. Every check here used to be a substring search over
`inspect.getsource(adapters.call)`. That is a mechanical proxy standing in for a judgement about what the
code does, and it fails in both directions: it passes when someone types the string in a comment or a dead
branch, and it fails when someone renames a local without changing behaviour. Neither has anything to do
with whether a request is correct.

Every check below now makes a real call against a recording transport and looks at what reached the wire.
The drop-and-retry is exercised by an endpoint that actually refuses the parameter, rather than asserted
about. No source text is inspected anywhere in this file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wire_recorder                                                        # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


# 1. EVERY OpenAI-compatible endpoint, enumerated from the provider table rather than listed here. A
#    hand-written vendor list would be the same defect as the model allow-list this guard exists to catch.
vendors = wire_recorder.openai_shaped_vendors()
check("there are OpenAI-shaped vendors to test at all", len(vendors) >= 3, str(vendors))
missed = []
for v in vendors:
    seen, _r, _n = wire_recorder.record(v, f"a-model-hosted-by-{v}", fact="high")
    if seen.get("reasoning_effort") != "high":
        missed.append((v, seen.get("reasoning_effort")))
check("a recorded effort reaches EVERY OpenAI-compatible endpoint, not an allow-list of model names",
      not missed,
      f"no control reached: {missed} — this is how the cost lever missed kimi-k3 and glm-5.2, "
      "the two models that reason the most")

# 2. NO INVENTED DEFAULT. A model nobody has measured sends nothing at all.
seen, _r, _n = wire_recorder.record("openai", "a-model-nobody-has-measured-and-no-rule-matches")
check("an unmeasured model sends NO effort — the vendor's own default applies",
      "reasoning_effort" not in seen,
      f"sent {seen.get('reasoning_effort')!r}; defaulting everything to 'minimal' measured as 10 output "
      "tokens and ZERO findings on a file where 'high' found the real bug")

# 3. THE DROP-AND-RETRY, exercised rather than asserted: an endpoint that refuses the parameter must still
#    get an answer, and the drop must be RECORDED — a silent drop is what blinds capability discovery.
seen, result, n = wire_recorder.record("openai", "an-endpoint-that-refuses-effort", fact="high",
                                       refuse="reasoning_effort")
check("an endpoint that REFUSES the parameter still gets its answer",
      not result.get("error") and n >= 2, f"error={str(result.get('error'))[:60]} requests={n}")
check("...and the drop is recorded, so 'it worked' can be told from 'it worked WITHOUT what you asked'",
      "reasoning_effort" in (result.get("dropped") or []), str(result.get("dropped")))
check("...and the retry actually went out without the refused parameter",
      "reasoning_effort" not in seen, str(seen.get("reasoning_effort")))

# 4. THE DECISION DOES NOT DEPEND ON THE MODEL ID. Two unrelated ids carrying the same fact must both get
#    it — which is what "no hardcoded model list" means operationally.
a, _r, _n = wire_recorder.record("openai", "some-model-alpha", fact="low")
b, _r, _n = wire_recorder.record("moonshot", "unrelated-model-beta", fact="low")
check("no model id is baked into the decision — the next reasoning model inherits it with no edit",
      a.get("reasoning_effort") == b.get("reasoning_effort") == "low",
      f"{a.get('reasoning_effort')!r} vs {b.get('reasoning_effort')!r}")


def test_the_registry_actually_answers_for_the_models_we_use():
    """The wiring is worthless if the registry has nothing to say. gpt-5.5's tier is a verified FAMILY fact
    and travels with the code; kimi-k3's and glm-5.2's were written by a measured A/B into the local fact
    store, so they exist only on a machine that has run it.

    An isolated SPENDGUARD_HOME therefore asserts NOTHING about the measured ones rather than failing — an
    empty store is a machine that has not run the A/B, not a registry that lost its answer. Asserting
    against it would make this guard fail on every fresh checkout and in CI, which teaches people to
    ignore it."""
    from spendguard import models
    got = (models.profile("gpt-5.5") or {}).get("reasoning")
    check("gpt-5.5 has a recorded effort (verified family fact, travels with the code)",
          bool(got) and got != "?", repr(got))
    for m in ("kimi-k3", "glm-5.2"):
        got = (models.profile(m) or {}).get("reasoning")
        if got and got != "?":
            check(f"{m} has the effort its A/B measured", True)
        else:
            print(f"  [OK] {m}: no local A/B result in this SPENDGUARD_HOME — asserted nothing")


test_the_registry_actually_answers_for_the_models_we_use()

print(f"\n{'[FAIL]' if failures else 'OK'} test_reasoning_control_reaches_every_endpoint: {failures} failure(s)")
sys.exit(1 if failures else 0)
