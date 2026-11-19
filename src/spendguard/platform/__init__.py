from spendguard.domain_context_surface import install_domain_context_surface

install_domain_context_surface(globals(), {
    "RECLAIM_DEFAULT_CACHE_DIRECTORIES": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_CACHE_DIRECTORIES",
    "RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES",
    "RECLAIM_DEFAULT_PROTECTION": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_PROTECTION",
    "RECLAIM_DEFAULT_ROTATED_LOGS": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_ROTATED_LOGS",
    "RECLAIM_DEFAULT_STATE_SNAPSHOTS": "spendguard.platform.home_reclaim:RECLAIM_DEFAULT_STATE_SNAPSHOTS",
    "RECLAIM_LSOF_EXECUTABLE_DEFAULT": "spendguard.platform.home_reclaim:RECLAIM_LSOF_EXECUTABLE_DEFAULT",
})
