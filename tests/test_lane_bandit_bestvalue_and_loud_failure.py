"""Offline regression guards, including negative controls for both original failures."""
from contextlib import contextmanager
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

BANDIT_GUARD_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(BANDIT_GUARD_SOURCE_ROOT))

from spendguard.analyze.learning import lane_bandit
from spendguard.store.call_corpus import call_corpus_accessors
from spendguard.store.reference_data import models, reference_data_accessors

BANDIT_GUARD_INTENT = "test:bandit-effort-resolution"
BANDIT_GUARD_LANE = "test-lane-executor"
BANDIT_GUARD_MODEL = "test-arm-model"
BANDIT_GUARD_PROVIDER = "test-arm-provider"
BANDIT_GUARD_MODULE = "test_arm_module"
BANDIT_GUARD_SENTINEL = "best-value"
BANDIT_GUARD_TASK = "Return the offline fixture reply."
BANDIT_GUARD_REPLY = "offline fixture reply"
BANDIT_GUARD_ERROR = "offline fixture rejected effort"
BANDIT_GUARD_ARM = (BANDIT_GUARD_LANE, BANDIT_GUARD_MODEL)


@contextmanager
def bandit_effort_guard_context():
    """Install an entirely offline executor and restore every patched target."""
    dispatch = Mock(return_value={"text": BANDIT_GUARD_REPLY})
    warnings = Mock()
    resolver = Mock(return_value=models._EFFORT_LADDER[-1])
    replacements = (
        (reference_data_accessors, "resolve_lane_provider", lambda lane: BANDIT_GUARD_PROVIDER),
        (lane_bandit._execution_accessors, "resolve__LANES",
         lambda: {BANDIT_GUARD_PROVIDER: (BANDIT_GUARD_LANE, BANDIT_GUARD_MODULE)}),
        (reference_data_accessors, "resolve_parse_use_name",
         lambda use_name, lane: (BANDIT_GUARD_MODEL, None)),
        (reference_data_accessors, "resolve_quirk", lambda lane: {"style": "param"}),
        (models, "effort_for", resolver),
        (call_corpus_accessors, "resolve_calls_record_call", Mock()),
        (lane_bandit.config, "warn_once", warnings),
    )
    originals = []
    module_key = f"spendguard.{BANDIT_GUARD_MODULE}"
    module_was_present = module_key in sys.modules
    original_module = sys.modules.get(module_key)
    try:
        for target, attribute, replacement in replacements:
            originals.append((target, attribute, getattr(target, attribute)))
            setattr(target, attribute, replacement)
        sys.modules[module_key] = SimpleNamespace(run_prompt=dispatch)
        yield SimpleNamespace(dispatch=dispatch, warnings=warnings, resolver=resolver)
    finally:
        if module_was_present:
            sys.modules[module_key] = original_module
        else:
            sys.modules.pop(module_key, None)
        for target, attribute, original in reversed(originals):
            setattr(target, attribute, original)


def assert_bandit_guard_resolves_effort(guard_context):
    result = lane_bandit._run_arm(BANDIT_GUARD_ARM, BANDIT_GUARD_TASK, None,
                                 BANDIT_GUARD_SENTINEL, None, BANDIT_GUARD_INTENT)
    assert result[0] == BANDIT_GUARD_REPLY
    received = guard_context.dispatch.call_args.kwargs["reasoning"]
    assert received != BANDIT_GUARD_SENTINEL
    assert received is None or received in models._EFFORT_LADDER


def assert_bandit_guard_warns_failure(guard_context):
    result = lane_bandit._run_arm(BANDIT_GUARD_ARM, BANDIT_GUARD_TASK, None,
                                 None, None, BANDIT_GUARD_INTENT)
    assert result == ("", 0)
    assert guard_context.warnings.called
    assert BANDIT_GUARD_LANE in guard_context.warnings.call_args.args[0]
    assert BANDIT_GUARD_MODEL in guard_context.warnings.call_args.args[0]


def check_bandit_sentinel_uses_pinned_measured_effort():
    with bandit_effort_guard_context() as context:
        assert_bandit_guard_resolves_effort(context)
        context.resolver.assert_called_once_with(BANDIT_GUARD_MODEL, BANDIT_GUARD_INTENT)
        assert context.dispatch.call_args.kwargs["model"] == BANDIT_GUARD_MODEL
        assert context.dispatch.call_args.kwargs["reasoning"] == context.resolver.return_value
    print("1 passed: best-value resolves measured effort with the model pinned")


def check_bandit_no_basis_uses_cli_default_and_warns():
    with bandit_effort_guard_context() as context:
        context.resolver.return_value = None
        assert_bandit_guard_resolves_effort(context)
        assert context.dispatch.call_args.kwargs["reasoning"] is None
        assert context.warnings.called
        assert BANDIT_GUARD_INTENT in context.warnings.call_args.args[0]
    print("1 passed: no measured basis uses CLI default and warns with intent")


def check_bandit_concrete_effort_passes_through():
    efforts = [None, *models._EFFORT_LADDER]
    for effort in efforts:
        with bandit_effort_guard_context() as context:
            lane_bandit._run_arm(BANDIT_GUARD_ARM, BANDIT_GUARD_TASK, None, effort, None, BANDIT_GUARD_INTENT)
            assert context.dispatch.call_args.kwargs["reasoning"] == effort
            context.resolver.assert_not_called()
    print(f"{len(efforts)} passed: concrete efforts and None bypass the resolver unchanged")


def check_bandit_failures_are_visible():
    failures = ("error", "exception", "blank", "invalid")
    for failure in failures:
        with bandit_effort_guard_context() as context:
            if failure == "exception":
                context.dispatch.side_effect = RuntimeError(BANDIT_GUARD_ERROR)
            else:
                context.dispatch.return_value = {"error": {"error": BANDIT_GUARD_ERROR},
                                                 "blank": {"text": " \n"}, "invalid": None}[failure]
            assert_bandit_guard_warns_failure(context)
            message = context.warnings.call_args.args[0]
            if failure in ("error", "exception"):
                assert BANDIT_GUARD_ERROR in message
    print(f"{len(failures)} passed: error, exception, blank and invalid replies warn visibly")


def check_bandit_raw_sentinel_negative_control():
    """The same guard must reject the original raw-sentinel forwarding behavior."""
    with bandit_effort_guard_context() as context:
        original_run_arm = lane_bandit._run_arm
        source = inspect.getsource(original_run_arm)
        mutant_source = source.replace("reasoning=eff)", "reasoning=(level or reasoning))")
        assert mutant_source != source
        namespace = dict(lane_bandit.__dict__)
        exec(compile(mutant_source, inspect.getfile(lane_bandit), "exec"), namespace)
        try:
            lane_bandit._run_arm = namespace["_run_arm"]
            try:
                assert_bandit_guard_resolves_effort(context)
            except AssertionError:
                pass
            else:
                raise AssertionError("raw-sentinel negative control did not fail the resolution guard")
        finally:
            lane_bandit._run_arm = original_run_arm
    print("1 passed: raw-sentinel negative control fails the resolution guard")


def check_bandit_silent_failure_negative_control():
    """Removing warning emission must fail the same error/exception assertions."""
    failure_modes = (False, True)
    for raises in failure_modes:
        with bandit_effort_guard_context() as context:
            if raises:
                context.dispatch.side_effect = RuntimeError(BANDIT_GUARD_ERROR)
            else:
                context.dispatch.return_value = {"error": BANDIT_GUARD_ERROR}
            original_run_arm = lane_bandit._run_arm
            namespace = dict(lane_bandit.__dict__)
            namespace["config"] = SimpleNamespace(warn_once=lambda *args, **kwargs: None)
            exec(compile(inspect.getsource(original_run_arm), inspect.getfile(lane_bandit), "exec"), namespace)
            try:
                lane_bandit._run_arm = namespace["_run_arm"]
                try:
                    assert_bandit_guard_warns_failure(context)
                except AssertionError:
                    pass
                else:
                    raise AssertionError("silent-failure negative control did not fail the warning guard")
            finally:
                lane_bandit._run_arm = original_run_arm
    print(f"{len(failure_modes)} passed: silent error and exception controls fail the warning guard")


if __name__ == "__main__":
    check_bandit_sentinel_uses_pinned_measured_effort()
    check_bandit_no_basis_uses_cli_default_and_warns()
    check_bandit_concrete_effort_passes_through()
    check_bandit_failures_are_visible()
    check_bandit_raw_sentinel_negative_control()
    check_bandit_silent_failure_negative_control()
