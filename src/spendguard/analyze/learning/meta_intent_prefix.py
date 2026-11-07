"""The meta-intent prefix constant — the one source, in a dependency-free module so both advisor and
intent_model_prior can read it without importing each other."""

META = "spendguard"   # intent prefix → routed to the meta budget by the gate
