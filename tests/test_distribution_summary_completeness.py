"""Every failed surface survives both capstone output formats, without sampling."""
import contextlib
import io
import json
from unittest.mock import patch

from spendguard import distribution

SUMMARY_COMPLETENESS_SURFACE_COUNT = 2048
SUMMARY_COMPLETENESS_TARGET = {"sha": "summary-fixture-sha", "version": "summary-fixture-version"}


def exercise_distribution_complete_summary_outputs():
    failed = [distribution.distribution_surface_row(f"summary-fixture:{index}", "api")
              for index in range(SUMMARY_COMPLETENESS_SURFACE_COUNT)]
    host = distribution.distribution_surface_row("host", "host", True, True)
    with contextlib.ExitStack() as stack:
        for owner, name, value in (
            (distribution, "resolve_distribution_target", SUMMARY_COMPLETENESS_TARGET),
            (distribution, "distribution_policy", {}),
            (distribution.config, "consumer_audit_settings", {}),
            (distribution.consumer_audit, "audit_consumer_workspaces", {"consumers": [], "pruned_at_depth": []}),
            (distribution.setup, "discover_distribution_interpreters", {"interpreters": [], "gaps": []}),
            (distribution, "attest_distribution_api_consumers", failed),
            (distribution, "attest_distribution_daemons", []),
            (distribution, "attest_distribution_host", host),
        ):
            stack.enter_context(patch.object(owner, name, return_value=value))
        result = distribution.distribute_fleet(check=True)
        expected = [row["surface"] for row in failed]
        assert result["exit_code"] != 0
        assert result["summary"] == {"converged": 1, "total": len(failed) + 1, "stragglers": expected}
        for arguments in (["--check", "--json"], ["--check"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert distribution.distribute_command(arguments) != 0
            rendered = output.getvalue()
            payload = json.loads(rendered) if "--json" in arguments else json.loads(rendered.splitlines()[-1])
            assert payload["summary"] == result["summary"]
            if "--json" not in arguments:
                assert len(rendered.splitlines()) == len(failed) + 3
        for missing_axis in ("on_version", "healthy", "fd_ok"):
            options = {"on_version": True, "healthy": True, "fd_ok": True}
            options[missing_axis] = False
            assert distribution.distribution_surface_row("missing-proof", "fixture", **options)["straggler_reason"]
    print("OK complete summary, both CLI formats, positive identity/health/fd proof required")


if __name__ == "__main__":
    exercise_distribution_complete_summary_outputs()
