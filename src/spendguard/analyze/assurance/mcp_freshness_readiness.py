"""Live MCP freshness and service ownership readiness evidence."""

from .capability_readiness import CapabilityReadinessEvidence


def assess_live_mcp_freshness_readiness(*, context):
    """Check the running server, never inferring a pass from missing evidence."""
    from ...platform import inspect_live_mcp_service, read_live_mcp_release_status

    try:
        status = read_live_mcp_release_status()
    except Exception as error:
        return CapabilityReadinessEvidence(
            "UNREVIEWED", f"Live MCP server is unreachable: {type(error).__name__}: {error}")
    served = status.get("served") or {}
    loaded_sha = served.get("sha")
    mode = status.get("install_kind") or (status.get("respawn") or {}).get("install_kind")
    service = inspect_live_mcp_service()
    if service.get("package_owned") is False:
        return CapabilityReadinessEvidence("FAIL", service.get("reason") or "MCP service is not package-owned",
                                           (f"label={service.get('label_used')}; package_owned=False",))
    if not loaded_sha or not mode:
        return CapabilityReadinessEvidence("UNREVIEWED", "Live MCP release status lacks loaded SHA or install mode")
    verdict = status.get("freshness")
    if (not isinstance(verdict, dict) or verdict.get("loaded_sha") != loaded_sha
            or verdict.get("mode") != mode or not verdict.get("source_sha")
            or verdict.get("fresh") is not (loaded_sha == verdict.get("source_sha"))):
        return CapabilityReadinessEvidence(
            "UNREVIEWED", "Live MCP release status lacks a consistent source freshness verdict")
    evidence = (f"mode={mode}; loaded_sha={loaded_sha}; source_sha={verdict.get('source_sha')}; "
                f"label={service.get('label_used')}; package_owned={service.get('package_owned')}",)
    if verdict.get("fresh") is False:
        return CapabilityReadinessEvidence("FAIL",
            f"MCP freshness: {verdict.get('reason')}; service ownership: {service.get('reason')}", evidence)
    if verdict.get("fresh") is not True or service.get("package_owned") is not True:
        return CapabilityReadinessEvidence("UNREVIEWED",
            f"MCP freshness or ownership could not be established: {verdict.get('reason')}; "
            f"{service.get('reason')}", evidence)
    return CapabilityReadinessEvidence("PASS", "Live MCP serves current source and is package-owned", evidence)
