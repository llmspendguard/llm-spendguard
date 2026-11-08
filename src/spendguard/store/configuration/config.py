"""Central config for spendguard — paths, knobs, key loading.

Decoupled from any host repo so the package is portable. Data (gate log, kill-switch
flag, reconcile cache) lives under SPENDGUARD_HOME (default ~/.spendguard). API keys
resolve from the environment first, then SPENDGUARD_ENV or ./.env.
"""
from spendguard.store.configuration.config_store import (  # absolute so runpy.run_path(config.__file__) can re-exec this module
    DEFAULT_SPENDGUARD_HOME as DEFAULT_SPENDGUARD_HOME,
    HOME as HOME,
    FLAG as FLAG,
    LOG as LOG,
    ANTHROPIC_CACHE as ANTHROPIC_CACHE,
    RT_LOG as RT_LOG,
    CONFIG_JSON as CONFIG_JSON,
    CODEX_POOL_SIZE_DEFAULT as CODEX_POOL_SIZE_DEFAULT,
    CODEX_DAEMON_MAX_WAITERS_DEFAULT as CODEX_DAEMON_MAX_WAITERS_DEFAULT,
    CODEX_DAEMON_REQUEST_READ_TIMEOUT_DEFAULT_S as CODEX_DAEMON_REQUEST_READ_TIMEOUT_DEFAULT_S,
    CODEX_DAEMON_RECYCLE_ON_TIMEOUT_DEFAULT as CODEX_DAEMON_RECYCLE_ON_TIMEOUT_DEFAULT,
    CODEX_DAEMON_LEASE_IDLE_S_DEFAULT as CODEX_DAEMON_LEASE_IDLE_S_DEFAULT,
    CODEX_DAEMON_LEASE_MAX_S_DEFAULT as CODEX_DAEMON_LEASE_MAX_S_DEFAULT,
    CODEX_POOL_ISOLATE_DEFAULT as CODEX_POOL_ISOLATE_DEFAULT,
    CODEX_POOL_HEALTH_INTERVAL_DEFAULT_S as CODEX_POOL_HEALTH_INTERVAL_DEFAULT_S,
    CODEX_POOL_RESPAWN_BACKOFF_DEFAULT_S as CODEX_POOL_RESPAWN_BACKOFF_DEFAULT_S,
    CODEX_POOL_RESPAWN_BACKOFF_MULTIPLIER as CODEX_POOL_RESPAWN_BACKOFF_MULTIPLIER,
    CODEX_POOL_OWNER_MARKER_FILENAME as CODEX_POOL_OWNER_MARKER_FILENAME,
    CODEX_POOL_HOME_STALE_AGE_DEFAULT_S as CODEX_POOL_HOME_STALE_AGE_DEFAULT_S,
    CODEX_POOL_TRUE_VALUES as CODEX_POOL_TRUE_VALUES,
    EXPLICIT_TRUE_VALUES as EXPLICIT_TRUE_VALUES,
    WORKSPACE_ROOTS_DEFAULT as WORKSPACE_ROOTS_DEFAULT,
    WORKSPACE_EXTRA_VENV_DIRS_DEFAULT as WORKSPACE_EXTRA_VENV_DIRS_DEFAULT,
    WORKSPACE_SCAN_DEPTH_DEFAULT as WORKSPACE_SCAN_DEPTH_DEFAULT,
    CONSUMER_AUDIT_EXEMPT_DEFAULT as CONSUMER_AUDIT_EXEMPT_DEFAULT,
    CODEX_SESSION_REAP_DEFAULTS as CODEX_SESSION_REAP_DEFAULTS,
    _GITROOT_CACHE as _GITROOT_CACHE,
    _scoped_intent_cap_overrides as _scoped_intent_cap_overrides,
    # NOTE: _scoped_intent_cap_lock is deliberately NOT re-exported. It is a fork-reset
    # mutable (new_fork_reset_ledger_state rebinds config_store's own global in the child);
    # a facade re-import would go stale after fork and defeat the ledger-fork-lock inventory.
    # Consumers reach it only via config.scoped_intent_cap(), which reads it bare in config_store.
    _WARNED_ONCE as _WARNED_ONCE,
    CODEX_LEASE_ACTIVE_ENV as CODEX_LEASE_ACTIVE_ENV,
    DISK_GUARD_FLOOR_DEFAULT_BYTES as DISK_GUARD_FLOOR_DEFAULT_BYTES,
    DISK_GUARD_FLOOR_ENV as DISK_GUARD_FLOOR_ENV,
    SPEND_DAILY_ROLLUP_WRITE_TIMEOUT_S as SPEND_DAILY_ROLLUP_WRITE_TIMEOUT_S,
    SPEND_DAILY_ROLLUP_AUDIT_INTERVAL_DEFAULT_SECONDS as SPEND_DAILY_ROLLUP_AUDIT_INTERVAL_DEFAULT_SECONDS,
    _json_mod as _json_mod,
    LEDGER_HOME_ENVIRONMENT_KEY as LEDGER_HOME_ENVIRONMENT_KEY,
    LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY as LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY,
    ConfigurationReadError as ConfigurationReadError,
    SingleInstanceLockUnavailableError as SingleInstanceLockUnavailableError,
    is_explicit_true as is_explicit_true,
    month_start_utc as month_start_utc,
    today_utc as today_utc,
    read_json_cache_entry as read_json_cache_entry,
    update_json as update_json,
    save_config as save_config,
    cfg_invalidate as cfg_invalidate,
    _cfg as _cfg,
    _cfg_get as _cfg_get,
    consumer_audit_settings as consumer_audit_settings,
    codex_session_reap_settings as codex_session_reap_settings,
    codex_daemon_pool_settings as codex_daemon_pool_settings,
    codex_daemon_admission_settings as codex_daemon_admission_settings,
    codex_daemon_worker_home_stale_age_s as codex_daemon_worker_home_stale_age_s,
    _advisor_coerced as _advisor_coerced,
    advisor_float as advisor_float,
    advisor_num as advisor_num,
    escalation_ratio as escalation_ratio,
    escalation_baseline_epsilon as escalation_baseline_epsilon,
    shed_lanes as shed_lanes,
    git_root_project as git_root_project,
    cap as cap,
    rt_budget as rt_budget,
    _policy_cap as _policy_cap,
    policy_caps as policy_caps,
    class_cap as class_cap,
    daily_cap as daily_cap,
    monthly_cap as monthly_cap,
    meta_cap as meta_cap,
    scoped_intent_cap as scoped_intent_cap,
    scoped_intent_authorizations as scoped_intent_authorizations,
    intent_cap as intent_cap,
    intent_cap_window_s as intent_cap_window_s,
    external_cap as external_cap,
    advisor_model as advisor_model,
    advisor_judge_model as advisor_judge_model,
    advisor_adjudicator_model as advisor_adjudicator_model,
    recall_model as recall_model,
    budget_backend as budget_backend,
    active_spendguard_home as active_spendguard_home,
    lane_queue_db_path as lane_queue_db_path,
    single_instance_lock as single_instance_lock,
    ssl_context as ssl_context,
    email_config as email_config,
    _project_saas as _project_saas,
    saas_config as saas_config,
    identity_path as identity_path,
    machine_id as machine_id,
    is_email as is_email,
    saas_path as saas_path,
    saas_state_path as saas_state_path,
    disabled as disabled,
    allow as allow,
    api_get as api_get,
    api_get_text as api_get_text,
    keep_backups_default as keep_backups_default,
    keep_corrupt_default as keep_corrupt_default,
    state_path as state_path,
    load_state as load_state,
    save_state as save_state,
    uid as uid,
    content_hash as content_hash,
    warn_once as warn_once,
    project_of_cwd as project_of_cwd,
    disk_guard_floor_bytes as disk_guard_floor_bytes,
    spend_daily_rollup_reporting_timezone as spend_daily_rollup_reporting_timezone,
    spend_daily_rollup_audit_interval_seconds as spend_daily_rollup_audit_interval_seconds,
)

from spendguard.store.configuration.ledger_connection import (  # absolute so runpy.run_path(config.__file__) can re-exec this module
    ISOLATED_TEST_ENVIRONMENT_KEY as ISOLATED_TEST_ENVIRONMENT_KEY,
    ISOLATED_TEST_ENABLED_VALUE as ISOLATED_TEST_ENABLED_VALUE,
    SQLITE_CONNECT_AUDIT_EVENT as SQLITE_CONNECT_AUDIT_EVENT,
    SQLITE_MEMORY_DATABASE_NAME as SQLITE_MEMORY_DATABASE_NAME,
    SQLITE_FILE_URI_SCHEME as SQLITE_FILE_URI_SCHEME,
    SQLITE_MEMORY_MODE_QUERY_KEY as SQLITE_MEMORY_MODE_QUERY_KEY,
    SQLITE_MEMORY_MODE_QUERY_VALUE as SQLITE_MEMORY_MODE_QUERY_VALUE,
    ISOLATED_SQLITE_ROOT_ENVIRONMENT_KEYS as ISOLATED_SQLITE_ROOT_ENVIRONMENT_KEYS,
    COVERAGE_DATA_FILE_ENVIRONMENT_KEY as COVERAGE_DATA_FILE_ENVIRONMENT_KEY,
    COVERAGE_DEFAULT_DATA_FILE_NAME as COVERAGE_DEFAULT_DATA_FILE_NAME,
    ISOLATED_SQLITE_IMPORT_HOME_ROOT as ISOLATED_SQLITE_IMPORT_HOME_ROOT,
    IsolatedSQLitePathError as IsolatedSQLitePathError,
    isolated_sqlite_target_path as isolated_sqlite_target_path,
    is_coverage_data_file_path as is_coverage_data_file_path,
    validate_isolated_sqlite_open_path as validate_isolated_sqlite_open_path,
    isolated_sqlite_connect_audit_hook as isolated_sqlite_connect_audit_hook,
    db_path as db_path,
    LEDGER_MAX_CONNECTIONS_DEFAULT as LEDGER_MAX_CONNECTIONS_DEFAULT,
    LEDGER_READ_POOL_CAP_DEFAULT as LEDGER_READ_POOL_CAP_DEFAULT,
    LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S as LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S,
    LEDGER_CACHE_BUDGET_KIB as LEDGER_CACHE_BUDGET_KIB,
    LEDGER_MMAP_BUDGET_BYTES as LEDGER_MMAP_BUDGET_BYTES,
    LEDGER_CONNECT_TIMEOUT_S as LEDGER_CONNECT_TIMEOUT_S,
    LEDGER_WRITE_TIMEOUT_S as LEDGER_WRITE_TIMEOUT_S,
    LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S as LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S,
    LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS as LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS,
    LEDGER_READ_TIME_BUDGET_S as LEDGER_READ_TIME_BUDGET_S,
    LEDGER_QUERY_DEADLINE_S as LEDGER_QUERY_DEADLINE_S,
    LEDGER_READ_ROW_CAP as LEDGER_READ_ROW_CAP,
    LEDGER_READ_PROGRESS_VM_INSTRUCTIONS as LEDGER_READ_PROGRESS_VM_INSTRUCTIONS,
    LEDGER_BUSY_TIMEOUT_MS as LEDGER_BUSY_TIMEOUT_MS,
    LEDGER_WAL_CHECKPOINT_INTERVAL_DEFAULT_S as LEDGER_WAL_CHECKPOINT_INTERVAL_DEFAULT_S,
    LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS as LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS,
    LEDGER_WAL_CHECKPOINT_CADENCE_WAIT_FRACTION as LEDGER_WAL_CHECKPOINT_CADENCE_WAIT_FRACTION,
    LEDGER_TIMEOUT_MS_PER_S as LEDGER_TIMEOUT_MS_PER_S,
    LEDGER_WAL_AUTOCHECKPOINT_PAGES as LEDGER_WAL_AUTOCHECKPOINT_PAGES,
    LEDGER_READ_BUSY_TIMEOUT_MS as LEDGER_READ_BUSY_TIMEOUT_MS,
    LEDGER_REQUEST_DISCONNECT_POLL_S as LEDGER_REQUEST_DISCONNECT_POLL_S,
    LEDGER_JOURNAL_LIMIT_BYTES as LEDGER_JOURNAL_LIMIT_BYTES,
    LEDGER_HOME_IDENTITY_CACHE_SIZE as LEDGER_HOME_IDENTITY_CACHE_SIZE,
    LedgerHomeIsolationError as LedgerHomeIsolationError,
    require_ledger_home_alignment as require_ledger_home_alignment,
    LedgerConnectionLimitError as LedgerConnectionLimitError,
    ledger_connection_limit as ledger_connection_limit,
    ledger_read_pool_settings as ledger_read_pool_settings,
    ledger_wal_checkpoint_interval_s as ledger_wal_checkpoint_interval_s,
    ledger_wal_checkpoint_busy_timeout_ms as ledger_wal_checkpoint_busy_timeout_ms,
    ledger_wal_autocheckpoint_pages as ledger_wal_autocheckpoint_pages,
    ledger_read_busy_timeout_ms as ledger_read_busy_timeout_ms,
    ledger_connection_count as ledger_connection_count,
    run_ledger_setup_pragmas as run_ledger_setup_pragmas,
    budget_ledger_connection_memory as budget_ledger_connection_memory,
    open_ledger_connection as open_ledger_connection,
    ensure_ledger_schema as ensure_ledger_schema,
    tune_ledger_connection as tune_ledger_connection,
    recycle_idle_ledger_connections as recycle_idle_ledger_connections,
    managed_ledger_read_connection as managed_ledger_read_connection,
    open_ledger_maintenance_connection as open_ledger_maintenance_connection,
    pooled_ledger_conn as pooled_ledger_conn,
    rollback_ledger_conn as rollback_ledger_conn,
    reset_ledger_conn as reset_ledger_conn,
    ledger_op as ledger_op,
    with_ledger_read_lease as with_ledger_read_lease,
    fresh_ledger_conn as fresh_ledger_conn,
)

import os
from pathlib import Path
from spendguard.store.configuration.config_accessors import set_config_accessors
from spendguard.store.configuration.ledger_connection_accessors import set_ledger_connection_accessors
from spendguard.store.configuration.provider_key_env_registry import (
    all_provider_key_envs,
)
from spendguard.store.configuration.provider_keys import (  # absolute so runpy.run_path(config.__file__) can re-exec this module
    KEYS_ENV as KEYS_ENV,
    api_key as api_key,
    dotenv_key_shadow_report as dotenv_key_shadow_report,
    key_fingerprint as key_fingerprint,
    key_shadow_report as key_shadow_report,
    key_source as key_source,
    load_key_files as load_key_files,
    provider_key_status as provider_key_status,
)


# Well-known user-local CLI install dirs, for hosts where PATH doesn't carry them (launchd/cron daemons run
# with a minimal PATH that misses ~/.local/bin and nvm's versioned bins — the subscription lanes must still
# find the plan CLIs there). Globs allowed; newest executable wins.
_CLI_SEARCH_DIRS = ("~/.claude/local", "~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin",
                    "~/.nvm/versions/node/*/bin", "~/.kimi-code/bin")   # Kimi Code CLI installs its own bin dir


def resolve_cli(name, env_var=None):
    """Absolute path of a host CLI or None: $<env_var> pin → PATH → well-known user-local dirs. An explicit
    env pin that doesn't exist returns None (fail LOUD at the pin, never silently substitute another binary)."""
    import shutil
    import glob as _glob
    pin = os.environ.get(env_var) if env_var else None
    if pin:
        return pin if (Path(pin).exists() and os.access(pin, os.X_OK)) else None
    w = shutil.which(name)
    if w:
        return w
    hits = []
    for d in _CLI_SEARCH_DIRS:
        for p in _glob.glob(str(Path(d).expanduser() / name)):
            if os.access(p, os.X_OK):
                hits.append(Path(p))
    # THE FILE CAN VANISH BETWEEN THE GLOB AND THE stat(). These directories include version-manager
    # trees (nvm and friends) that rewrite themselves as a version is installed or removed, so a hit that
    # existed a microsecond ago may be gone — and an unguarded stat() raised FileNotFoundError out of a
    # function whose whole job is "find the CLI, or return None". A disappeared candidate sorts LAST
    # rather than killing the search: it is one fewer option, not an error.
    def _mtime(c):
        try:
            return c.stat().st_mtime
        except OSError:
            return -1.0
    return str(max(hits, key=_mtime)) if hits else None


def lane_plan_env(keep=()):
    """Child env for a subscription-LANE subprocess: os.environ with EVERY provider's METERED api-key env var
    REMOVED, except names in `keep` (a lane that authenticates with a plan-specific token, not a plan login).

    This is the hard, STRUCTURAL guarantee behind "$0 billed on a lane, no double-usage": a lane subprocess that
    carries no metered key cannot make a metered API call at all — and, above all, a NON-Claude lane can never
    inherit ANTHROPIC_API_KEY and silently spend Claude tokens for work that was meant to ride another plan. (keys.env
    is loaded into os.environ at import, so WITHOUT this scrub a codex/gemini subprocess would inherit every key.)

    Key names come from the provider REGISTRY (so a newly registered provider is covered with no edit here) plus the
    auth-token / base-url variants that re-authenticate or relocate the same billing. No prices, no behaviour — just
    the set of env vars that authorize metered spend."""
    keep = set(keep or ())
    # Both metered API keys AND flat-fee PLAN tokens: a lane carries only the credential(s) it owns (via keep), so a
    # codex/gemini lane holds no zai coding token either. ZAI_CODING_API_KEY is a plan token, not metered, but a lane
    # that does not own it has no business carrying it.
    metered = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "OPENAI_API_KEY", "OPENAI_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY",
               "ZAI_API_KEY", "ZAI_CODING_API_KEY"}
    metered |= all_provider_key_envs()
    metered = {m for m in metered if m} - keep
    return {k: v for k, v in os.environ.items() if k not in metered}


CLI_TEST_OFFLINE_ENVIRONMENT_KEY = "SPENDGUARD_TEST_CLI_OFFLINE"
CLI_TEST_OFFLINE_ENABLED_VALUE = "1"


class IsolatedCliLaunchError(BaseException):
    """Escape fail-soft lane handlers when a test attempts a real CLI launch."""


def refuse_isolated_cli_launch():
    if os.environ.get(CLI_TEST_OFFLINE_ENVIRONMENT_KEY) == CLI_TEST_OFFLINE_ENABLED_VALUE:
        raise IsolatedCliLaunchError(
            f"{CLI_TEST_OFFLINE_ENVIRONMENT_KEY} forbids a real subscription CLI launch; supply a lane/usage fixture")


def lane_exec_env(cli_path, keep=()):
    """Plan-safe child env for a resolved subscription CLI, with its sibling runtime directory first on PATH.

    Node-installed CLIs are commonly shebang scripts whose ``node`` executable lives beside the resolved CLI
    (not on launchd's minimal PATH). Deriving the directory from the resolver's absolute result keeps both probes
    and real calls runnable without encoding any version-manager layout. ``lane_plan_env`` remains the credential
    boundary; this helper changes only PATH and never makes a missing CLI look present.
    """
    refuse_isolated_cli_launch()
    env = lane_plan_env(keep=keep)
    if cli_path:
        cli_dir = os.path.dirname(os.path.abspath(cli_path))
        env["PATH"] = cli_dir + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    return env


# ── per-adapter state files ──────────────────────────────────────────────────────────────────────────────


# Expose config's state/value accessors to modules that must not import config — breaks the
# resource_state/route_realized/disk_guard→config cycles (all read via the config_accessors leaf).
set_config_accessors(load_state, save_state, _cfg_get, db_path, disk_guard_floor_bytes, ledger_op, pooled_ledger_conn, uid,
                     lambda: HOME, ssl_context, api_key, KEYS_ENV, warn_once)
set_ledger_connection_accessors(
    open_ledger_connection, db_path, ledger_wal_checkpoint_interval_s,
    ledger_wal_checkpoint_busy_timeout_ms, LEDGER_READ_TIME_BUDGET_S,
    LEDGER_TIMEOUT_MS_PER_S, LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS,
    LedgerConnectionLimitError,
)


# Cache publication never waits behind a spend writer; the authoritative heap is available.
