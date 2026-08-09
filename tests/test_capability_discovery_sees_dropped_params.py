"""Discovery must see a DROPPED parameter, or it reports a capability nobody has.

WHY THIS GUARD EXISTS. adapters.call has a fallback: if an endpoint rejects `reasoning_effort`, it drops the
parameter and retries, so the call succeeds. That fallback is right — it keeps a non-reasoning endpoint
working. But it made the first capability probe report that ALL FOUR vendors supported ALL SIX effort tiers,
including anthropic (whose request shape has no such parameter at all) and z.ai (which had rejected `auto`
in a direct test minutes earlier). The probe was measuring "did the call eventually work", not "was the
value accepted".

The fallback that keeps the system robust is exactly what blinds the probe. So a drop is now RECORDED on the
result, and a capability nobody can exercise is never reported as present.

Ground truth after the fix, all probed and none hardcoded:
    anthropic   n/a — the anthropic request shape carries no reasoning_effort
    gpt-5.5     none,minimal,low,medium,high,auto
    kimi-k3     none,minimal,low,medium,high,auto
    glm-5.2     none,minimal,low,medium,high        (auto REJECTED)
"""
import inspect
import sys

from spendguard import adapters, vendor_call as vc

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_a_dropped_parameter_is_reported_on_the_result():
    src = inspect.getsource(adapters.call)
    check("dropping reasoning_effort is recorded on the result, not silent",
          'dropped' in src and 'pop("reasoning_effort"' in src,
          "a silently-dropped parameter makes every capability probe answer yes")


def test_discovery_counts_a_drop_as_a_rejection():
    src = inspect.getsource(vc.discover_efforts)
    check("discovery inspects `dropped` before believing a success",
          '"reasoning_effort" in (r.get("dropped")' in src,
          "otherwise a retried call is indistinguishable from an accepted parameter")


def test_a_provider_that_cannot_carry_the_param_is_NOT_reported_as_supporting_it():
    """Absence of an error is not evidence of a capability. anthropic never sends this parameter, so the
    honest answer is 'not applicable' — not 'accepts everything'."""
    src = inspect.getsource(vc.discover_efforts)
    check("non-openai request shapes return not_applicable",
          'not_applicable' in src and 'kind != "openai"' in src)
    d = vc.discover_efforts("anthropic", "claude-opus-4-8")
    check("...and anthropic is recorded that way, from the cache", bool(d.get("not_applicable")), str(d)[:80])


def test_an_unmeasured_class_gets_NO_effort_rather_than_an_invented_one():
    eff, basis = vc.effort_policy("zai", "glm-5.2", sig="a-class-nobody-has-measured")
    check("unmeasured -> (None, 'unmeasured')", (eff, basis) == (None, "unmeasured"), f"{eff} {basis}")


def test_transport_failures_are_unknown_not_rejections():
    """A network blip says nothing about whether a value is supported. Recording it as a rejection would
    permanently mark a working capability as unavailable."""
    src = inspect.getsource(vc.discover_efforts)
    check("a non-effort error is recorded as unknown, never as rejected", "unknown.append(eff)" in src)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_capability_discovery_sees_dropped_params: {failures} failure(s)")
    sys.exit(1 if failures else 0)
