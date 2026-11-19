from spendguard.domain_context_surface import install_domain_context_surface

install_domain_context_surface(globals(), {
    "current_durable_execution_id": "spendguard.manage.token_operations.execution_journal:current_durable_execution_id",
})
