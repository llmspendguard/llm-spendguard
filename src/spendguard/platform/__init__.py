from spendguard.domain_context_surface import install_domain_context_surface

install_domain_context_surface(globals(), {
    "RECLAIM_DEFAULT_CACHE_DIRECTORIES": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_CACHE_DIRECTORIES",
    "RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES",
    "RECLAIM_DEFAULT_PROTECTION": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_PROTECTION",
    "RECLAIM_DEFAULT_ROTATED_LOGS": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_ROTATED_LOGS",
    "RECLAIM_DEFAULT_STATE_SNAPSHOTS": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_STATE_SNAPSHOTS",
    "RECLAIM_LSOF_EXECUTABLE_DEFAULT": "spendguard.platform.home_reclaim:RECLAIM_LSOF_EXECUTABLE_DEFAULT",
    "restart_and_verify_live_mcp": "spendguard.platform.distribution_daemon:restart_and_verify_live_mcp",
    "inspect_live_mcp_service": "spendguard.platform.distribution_daemon:inspect_live_mcp_service",
    "read_live_mcp_release_status": "spendguard.platform.distribution_daemon:read_live_mcp_release_status",
})
