"""Delegate-boundary visibility for serving state and governed-route failures."""
import contextlib
import io
from unittest.mock import patch

from spendguard import delegate_router


BOUNDARY_TEST_TASK = "perform the self-contained fixture task"
BOUNDARY_TEST_PROVIDER = "codex"
BOUNDARY_TEST_UNDERLYING_ERROR = "fixture executor could not launch"
BOUNDARY_TEST_SERVED_DESCRIPTION = "fixture-dirty-description"
BOUNDARY_TEST_SERVED_SHORT = "served1"
BOUNDARY_TEST_GREEN_SHORT = "green22"


def _boundary_estimate_result():
    return {
        "status": "estimate",
        "classification": {
            "kind": "agentic",
            "provider": BOUNDARY_TEST_PROVIDER,
            "why": "self-contained fixture",
        },
        "estimate": {
            "plan": BOUNDARY_TEST_PROVIDER,
            "model": "fixture-model",
            "saved_overage_usd": None,
            "real_api_usd": 0.0,
            "est_value_usd": 0.0,
        },
    }


def test_delegate_dirty_stale_serving_tree_emits_release_warning():
    release_status = {
        "served": {"describe": BOUNDARY_TEST_SERVED_DESCRIPTION, "short": BOUNDARY_TEST_SERVED_SHORT},
        "green": {"short": BOUNDARY_TEST_GREEN_SHORT},
        "served_dirty": True,
        "stale": True,
    }
    output_stream = io.StringIO()
    with contextlib.redirect_stderr(output_stream), \
            patch.object(delegate_router._release, "release_status", return_value=release_status), \
            patch.object(delegate_router._release, "_tracked_dirty", return_value=True), \
            patch.object(delegate_router, "delegate_task", return_value=_boundary_estimate_result()), \
            patch("spendguard.fleet.register_consumer"):
        exit_code = delegate_router.delegate_cli([BOUNDARY_TEST_TASK, "--provider", BOUNDARY_TEST_PROVIDER])
    output = output_stream.getvalue()
    assert exit_code == 0
    assert "⚠ delegate served from uncommitted/stale spendguard" in output
    assert BOUNDARY_TEST_SERVED_DESCRIPTION in output
    assert BOUNDARY_TEST_GREEN_SHORT in output


def test_delegate_clean_on_green_serving_tree_emits_no_release_warning():
    release_status = {
        "served": {"describe": BOUNDARY_TEST_SERVED_DESCRIPTION, "short": BOUNDARY_TEST_GREEN_SHORT},
        "green": {"short": BOUNDARY_TEST_GREEN_SHORT},
        "served_dirty": False,
        "stale": False,
    }
    output_stream = io.StringIO()
    with contextlib.redirect_stderr(output_stream), \
            patch.object(delegate_router._release, "release_status", return_value=release_status), \
            patch.object(delegate_router._release, "_tracked_dirty", return_value=False), \
            patch.object(delegate_router, "delegate_task", return_value=_boundary_estimate_result()), \
            patch("spendguard.fleet.register_consumer"):
        exit_code = delegate_router.delegate_cli([BOUNDARY_TEST_TASK, "--provider", BOUNDARY_TEST_PROVIDER])
    output = output_stream.getvalue()
    assert exit_code == 0
    assert "delegate served from" not in output


def test_delegate_codex_launch_failure_names_ungoverned_attribution_loss():
    failed_result = {
        "status": "error",
        "classification": {
            "kind": "agentic",
            "provider": BOUNDARY_TEST_PROVIDER,
            "why": "self-contained fixture",
        },
        "execution": {"error": BOUNDARY_TEST_UNDERLYING_ERROR},
        "reason": delegate_router.DELEGATE_EXECUTION_REFUSED,
        "why": BOUNDARY_TEST_UNDERLYING_ERROR,
    }
    output_stream = io.StringIO()
    with contextlib.redirect_stderr(output_stream), \
            patch.object(delegate_router, "_delegate_release_state_warning", return_value=None), \
            patch.object(delegate_router, "delegate_task", return_value=failed_result), \
            patch("spendguard.fleet.register_consumer"):
        exit_code = delegate_router.delegate_cli([BOUNDARY_TEST_TASK, "--provider", BOUNDARY_TEST_PROVIDER])
    output = output_stream.getvalue()
    assert exit_code == 2
    assert BOUNDARY_TEST_UNDERLYING_ERROR in output
    assert "governed Codex delegation route unavailable" in output
    assert delegate_router.DELEGATE_UNGOVERNED_CODEX_FALLBACK in output
    assert "outside spendguard's gate and ledger" in output
    assert "cost attribution is lost" in output


if __name__ == "__main__":
    test_delegate_dirty_stale_serving_tree_emits_release_warning()
    test_delegate_clean_on_green_serving_tree_emits_no_release_warning()
    test_delegate_codex_launch_failure_names_ungoverned_attribution_loss()
    print("OK test_delegate_boundary_visibility")
