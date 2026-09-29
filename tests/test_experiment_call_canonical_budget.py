"""Guard: experiment._call sends the CANONICAL output budget + normalized reasoning, not a hand-picked cap.

_call used to send a hand-picked max_tokens (max_out default 400; callers passed 1500 / max(1500, tokens*2+800)) and
write the raw `effort` straight to reasoning_effort — re-implementing two registered concerns (adapters.output_budget
and models.normalize_reasoning) and, per the doctrine, quoting a budget nobody measured. Now the wire budget IS
adapters.output_budget(model) (billed on ACTUAL tokens, so the ceiling is free and never truncates a measured answer)
and an explicit effort is resolved through normalize_reasoning (dropped when it returns None). This pins both, so a
future hand-picked cap or raw-effort literal fails loudly.

Offline, $0: the OpenAI SDK is faked to capture the wire kwargs. Isolated SPENDGUARD_HOME.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-experiment-call-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")     # the fake client ignores it; resolves the key lookup
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import openai                                                    # noqa: E402
from spendguard import experiment, adapters, models             # noqa: E402

MODEL = "gpt-5-nano"                                             # a real OpenAI reasoning-capable model (priced)


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    captured = {}

    class _Usage:
        prompt_tokens = 5
        completion_tokens = 3

    class _Msg:
        content = "an answer"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        def create(self, **kw):
            captured.clear()
            captured.update(kw)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeOpenAI:
        def __init__(self, **k):
            self.chat = _Chat()

    _real = openai.OpenAI
    openai.OpenAI = _FakeOpenAI
    try:
        experiment._call(MODEL, "measure this", effort="minimal")

        # the wire budget is the CANONICAL output_budget (under max_tokens or max_completion_tokens, per the model's
        # tokens param) — NOT a hand-picked 400/1500.
        wire = captured.get("max_tokens", captured.get("max_completion_tokens"))
        exp_budget = adapters.output_budget(MODEL, vendor="openai")
        ck("wire output budget == adapters.output_budget(model) (no hand-picked cap)", wire == exp_budget,
           (wire, exp_budget))
        ck("the budget is NOT the old hardcodes (400 / 1500)", wire not in (400, 1500), wire)

        # an explicit effort is resolved through normalize_reasoning (the reasoning home), dropped when it returns None.
        exp_eff = models.normalize_reasoning(MODEL, "minimal")
        if exp_eff is None:
            ck("effort that normalizes to None is dropped (no raw literal sent)", "reasoning_effort" not in captured,
               captured.get("reasoning_effort"))
        else:
            ck("reasoning_effort == normalize_reasoning(model, 'minimal') (canonical, not the raw literal)",
               captured.get("reasoning_effort") == exp_eff, (captured.get("reasoning_effort"), exp_eff))
    finally:
        openai.OpenAI = _real

    print(f"\n{'[FAIL]' if fails else 'OK'} test_experiment_call_canonical_budget: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
