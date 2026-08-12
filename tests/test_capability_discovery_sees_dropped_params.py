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
    # THE REQUEST BUILDER MOVED. `adapters.call` is now the guarded entry point (input-size check +
    # truncation retry) and `_call_once` builds and sends the request. These checks are about the
    # request, so they read the builder. Reading `call` here would inspect the guard wrapper and
    # pass vacuously — a source-reading test silently detaches from its subject when code moves.
    src = inspect.getsource(adapters._call_once)
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
    # Read the CACHE, never discover_efforts() — that probes live on a miss, and a test must not spend.
    # In an isolated HOME the cache is empty, which is a valid state to assert nothing about.
    cached = (vc.caps().get("anthropic/claude-opus-4-8") or {}).get("efforts")
    check("...and anthropic is cached that way when a cache exists",
          cached is None or bool(cached.get("not_applicable")), str(cached)[:80])


def test_an_unmeasured_model_gets_NO_effort_rather_than_an_invented_one():
    """The PROPERTY has to outlive the module it was written in.

    This used to call vendor_call.effort_policy(), one of two competing effort registries. That pair was
    deleted (see test_one_place_decides_per_model_params.py — per-model POLICY lives in models.py; the caps
    file here holds measured LIMITS), so the test now asks the surviving store the same question.

    The answer must still be silence. Sending an invented tier is not a conservative default: measured,
    glm-5.2 reviewing calls.py at `minimal` returned 10 output tokens and ZERO findings where `high`
    returned 793 and found the real bug. Nothing is the only honest thing to send about a model nobody has
    measured — the vendor's own default is at least chosen by someone who knows the model."""
    from spendguard import models
    kw = models.apply_call_params("a-model-nobody-has-measured-and-no-rule-matches",
                                  {"model": "x"}, dialect="openai")
    check("unmeasured -> nothing sent, vendor default applies",
          "reasoning_effort" not in kw, str(kw))


def test_discovery_forces_the_API_path():
    """A subscription lane serves the prompt WITHOUT sending the provider's parameters, so every probe comes
    back clean and discovery concludes the endpoint supports everything. Measured: gpt-5.5 was reported as
    accepting `minimal` because the codex lane answered it; the API rejects `minimal` with a 400, exactly as
    models.py's verified note has said all along. Third time in this project a lane silently invalidated a
    measurement — a capability probe belongs on the path whose capability is in question."""
    src = inspect.getsource(vc.discover_efforts)
    check("discovery pins the executor to the API before probing",
          'SPENDGUARD_ADVISOR_EXECUTOR"] = "api"' in src)
    check("...and restores the caller's setting afterwards",
          "finally:" in src and "_prev" in src,
          "a probe must not leave the process routing differently than it found it")


def test_discovery_agrees_with_the_independently_verified_registry():
    """models.py carries a VERIFIED fact: gpt-5.5 rejects 'minimal'. Two independent sources of truth about
    the same endpoint must not disagree — when they did, the probe was wrong."""
    from spendguard import models
    d = (vc.caps().get("openai/gpt-5.5") or {}).get("efforts") or {}
    if not d or not d.get("accepted"):
        check("no cached probe to cross-check (isolated run) — asserted nothing", True)
        return
    want = (models.profile("gpt-5.5") or {}).get("reasoning")
    if not want or want == "?":
        check("registry has no verified tier to cross-check against", True)
        return
    # BOTH SIDES STRUCTURED. This used to grep the family rule's English `note` for the word "REJECT" —
    # reading a fact out of prose, which is the very shape of bug this file exists to catch. The note says
    # "'minimal' is REJECTED (400)" and the only way to consume it was a substring search that would go
    # silently false the moment someone rewrote the sentence, taking the cross-check with it.
    #
    # The structured question is also the more useful one: the tier the registry tells every call to SEND
    # must be one the endpoint was MEASURED to accept. That is the failure with teeth — a registry pointing
    # production at a tier the API 400s on.
    check("the tier the registry says to SEND is one the endpoint was measured to ACCEPT",
          want in (d.get("accepted") or []),
          f"registry says send {want!r}; probe measured accepted={d.get('accepted')} "
          f"rejected={d.get('rejected')}")


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
