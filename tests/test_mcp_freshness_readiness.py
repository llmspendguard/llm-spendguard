"""Offline coverage for live MCP readiness evidence."""
from spendguard.analyze.assurance.mcp_freshness_readiness import assess_live_mcp_freshness_readiness
from spendguard import platform
from unittest.mock import patch


def test_live_mcp_readiness_requires_fresh_owned_service():
    verdict = {"fresh": True, "mode": "editable", "loaded_sha": "loaded",
               "source_sha": "loaded", "reason": "matching"}
    status = {"served": {"sha": "loaded"}, "install_kind": "editable", "freshness": verdict}
    service = {"package_owned": True, "label_used": "test-service", "reason": "registered"}
    with patch.object(platform, "read_live_mcp_release_status", return_value=status), \
         patch.object(platform, "inspect_live_mcp_service", return_value=service):
        assert assess_live_mcp_freshness_readiness(context={}).status == "PASS"
        verdict.update(fresh=False, source_sha="new", reason="different")
        assert assess_live_mcp_freshness_readiness(context={}).status == "FAIL"
        verdict.update(fresh=True, source_sha="loaded")
        service.update(package_owned=False, reason="not package-owned")
        assert assess_live_mcp_freshness_readiness(context={}).status == "FAIL"
        service.update(package_owned=None, reason="unknown")
        assert assess_live_mcp_freshness_readiness(context={}).status == "UNREVIEWED"
        verdict.update(loaded_sha="inconsistent")
        assert assess_live_mcp_freshness_readiness(context={}).status == "UNREVIEWED"


def test_live_mcp_readiness_unreachable_is_unreviewed():
    with patch.object(platform, "read_live_mcp_release_status", side_effect=OSError("unreachable")):
        assert assess_live_mcp_freshness_readiness(context={}).status == "UNREVIEWED"


if __name__ == "__main__":
    test_live_mcp_readiness_requires_fresh_owned_service()
    test_live_mcp_readiness_unreachable_is_unreviewed()
    print("MCP freshness readiness: PASS")
