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

The guard was also unnecessary: the retry path already drops the parameter for any endpoint that refuses it.

BUT THE FIRST FIX WAS ALSO WRONG, and the quality measurement is why. Replacing the regex with
`reasoning or "minimal"` swapped one hand-picked bound for another, and MEASURED it destroys the work:

    calls.py   glm-5.2  minimal ->    10 output tokens,  0 findings
    calls.py   glm-5.2  high    ->   793 output tokens,  1 finding (the real naive-timestamp bug)
    pricing.py kimi-k3  minimal ->  1,522 tokens, 3 findings   <- MORE than high, so not even monotonic
    pricing.py kimi-k3  high    ->  3,848 tokens, 2 findings

The "96x cheaper" was 96x cheaper because it had stopped reviewing. Effort is a property of
(call-class, model), settled by measurement like max_tokens and the deadline — and until a class HAS a
measurement, nothing is sent and the vendor's own default applies. An invented bound is worse than no
bound: no bound is at least honest about being unmeasured.
"""
import inspect
import re
import sys

from spendguard import adapters

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


src = inspect.getsource(adapters.call)

check("reasoning_effort is set for every OpenAI-compatible endpoint, not a model allow-list",
      "reasoning_effort" in src and not re.search(r"if\s+re\.match\(.*gpt-5.*\)\s*:", src),
      "a hardcoded model pattern gives the control to the models that need it least")

check("the drop-and-retry for endpoints that refuse it still exists",
      'reasoning_effort' in src and 'pop("reasoning_effort"' in src,
      "without it, a non-reasoning endpoint would 400 instead of silently proceeding")

check("no INVENTED default — the parameter is sent only when a caller asks",
      "reasoning or " not in src and "if reasoning:" in src,
      'defaulting everything to "minimal" is a hand-picked bound, and measurement showed it destroys the '
      'work: glm-5.2 reviewing calls.py at minimal returned 10 tokens and ZERO findings')

# No model id may be hardcoded into the reasoning decision — a new reasoning model must inherit the control
# on the day it is added, without anyone editing a regex.
region = src[max(0, src.find("reasoning_effort") - 900):src.find("reasoning_effort") + 200]
check("no model id is hardcoded into the decision",
      not re.search(r"\"(gpt-5|o1|o3|kimi|glm)[^\"]*\"\s*\)?\s*:", region)
      and "re.match" not in region.split("reasoning_effort")[0][-300:],
      "the next reasoning model must inherit this without an edit")

print(f"\n{'[FAIL]' if failures else 'OK'} test_reasoning_control_reaches_every_endpoint: {failures} failure(s)")
sys.exit(1 if failures else 0)
