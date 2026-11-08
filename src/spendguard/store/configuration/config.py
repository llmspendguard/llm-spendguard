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
    resolve_provider_key_env,
)
KEYS_ENV = HOME / "keys.env"                    # SECRETS: LLM/compute/org keys (KEY=value lines), loaded at import



def _iter_env_file(path):
    """Yield (key, value) from a KEY=value dotenv file — tolerant of comments, blanks, `export `, and quotes.
    Never raises (financial setup must not crash on a malformed line)."""
    try:
        if not path.exists():
            return
        for ln in path.read_text().splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            yield k, v.strip().strip('"').strip("'")
    except Exception:
        return


_KEYS_SET_BY_SPENDGUARD = set()   # vars WE set from the key files this process — a profile may override
                                  # these; a var the USER'S environment set is never touched.


def _key_profile():
    """Active key profile name: $SPENDGUARD_KEY_PROFILE, else the repo's `.spendguard.json` `key_profile`.
    None = no profile (unsuffixed keys only)."""
    v = os.environ.get("SPENDGUARD_KEY_PROFILE")
    if v:
        return v.strip() or None
    try:
        return (saas_config().get("key_profile") or "").strip() or None
    except Exception:
        return None


def load_key_files():
    """Load the secret files (~/.spendguard/keys.env, then legacy ~/.spendguard/.env, then $SPENDGUARD_ENV) into
    os.environ, so BOTH spendguard AND the user's OWN clients — openai.OpenAI() / anthropic.Anthropic(), which
    read their key from the environment — pick the keys up after a plain `import spendguard`. A REAL environment
    variable ALWAYS wins (a set var is never overwritten) and blank placeholders are skipped, so prod / CI /
    secret-managers are never clobbered. Idempotent; fail-open (never raises at import).

    KEY PROFILES (per-repo key selection): one global keys.env can hold every workspace/project-scoped key as
    `<VAR>__<profile>` entries (e.g. `ANTHROPIC_API_KEY__lmm=…`). When a profile is active (`key_profile` in the
    repo's .spendguard.json, or $SPENDGUARD_KEY_PROFILE), those entries override the unsuffixed defaults for
    their base var — so each repo picks its own provider key without editing any source or per-repo secrets.
    Precedence, highest first: real environment → active profile entry → unsuffixed file entry. Suffixed
    entries never leak into env under any other (or no) profile."""
    entries = {}
    for p in (KEYS_ENV, HOME / ".env", *( [Path(os.environ["SPENDGUARD_ENV"])] if os.environ.get("SPENDGUARD_ENV") else [] )):
        for k, v in _iter_env_file(p):
            if k and v and k not in entries:             # first file wins per var (keys.env over legacy .env)
                entries[k] = v
    for k, v in entries.items():
        if "__" in k:
            continue                                     # profile entries apply only via their own profile below
        if k not in os.environ:
            os.environ[k] = v
            _KEYS_SET_BY_SPENDGUARD.add(k)
    prof = _key_profile()
    if prof:
        suffix = "__" + prof
        for k, v in entries.items():
            if not k.endswith(suffix):
                continue
            base = k[: -len(suffix)]
            if not base or (base in os.environ and base not in _KEYS_SET_BY_SPENDGUARD):
                continue                                 # a REAL environment variable always wins
            os.environ[base] = v
            _KEYS_SET_BY_SPENDGUARD.add(base)


def key_shadow_report():
    """Provider keys whose ACTIVE value (os.environ) DIFFERS from what keys.env declares — i.e. an EXTERNAL source
    (the process env, or a repo's OWN .env loaded into it) is SHADOWING keys.env. A shadow is not always wrong (a real
    shell export, or an active key PROFILE — both EXCLUDED here), but a STALE shadow is invisible and is exactly how a
    rotated key keeps 401ing: a repo's .env held the dead key after keys.env was updated, and a real env var always wins
    in api_key(). Read-only, $0, LAST-4 only (never the secret). Returns [{name, active4, declared4}] — the override in
    effect vs what keys.env says. The intended way to run a repo on a DIFFERENT key is a declared profile
    (`<VAR>__<profile>` in keys.env + `key_profile` in the repo's .spendguard.json), which this never flags."""
    declared = {}
    for k, v in _iter_env_file(KEYS_ENV):
        if k and v and "__" not in k:
            declared.setdefault(k, v)                     # first wins, matching load_key_files order
    prof = _key_profile()
    prof_declared = {}
    if prof:
        _suf = "__" + prof
        for k, v in _iter_env_file(KEYS_ENV):
            if k.endswith(_suf):
                prof_declared[k[: -len(_suf)]] = v
    out = []
    for name, dval in declared.items():
        if not name.endswith(("_API_KEY", "_KEY", "_TOKEN")):
            continue                                      # provider/compute keys only, never arbitrary config
        active = os.environ.get(name)
        if not active or active == dval:
            continue                                      # no override in effect, or it matches the keys.env default
        if name in _KEYS_SET_BY_SPENDGUARD:
            continue                                      # spendguard set it (from a declared profile) — not an external shadow
        if name in prof_declared and active == prof_declared[name]:
            continue                                      # a DECLARED active-profile override — intentional + visible
        out.append({"name": name, "active4": active[-4:], "declared4": dval[-4:]})
    return out


# The .env-family filenames a repo might hold a shadowing provider key in; scanned when a DIRECTORY is given.
# Non-recursive on purpose — a repo keeps these at its root, and a deep walk would read unrelated vendored fixtures.
_DOTENV_SCAN_NAMES = (".env", ".env.local", ".env.development", ".env.development.local",
                      ".env.production", ".env.production.local", ".env.test", ".env.test.local")


def _dotenv_scan_files(paths):
    """The .env-family files an audit of `paths` will actually read: each directory expanded (non-recursive) to the
    known .env names it contains, each explicit file kept, existence-checked and de-duplicated. The ONE walk both
    dotenv_key_shadow_report() and the keys-audit CLI share, so the 'files scanned' count can never drift from what
    was judged. Never raises per path."""
    files, seen = [], set()
    for p in paths or []:
        try:
            pp = Path(p).expanduser()
            cands = [pp / n for n in _DOTENV_SCAN_NAMES] if pp.is_dir() else [pp]
        except Exception:
            continue                                      # an unreadable path is skipped, never crashes the audit
        for f in cands:
            rp = str(f)
            try:
                ok = f.is_file()
            except Exception:
                ok = False
            if ok and rp not in seen:                     # a file named twice (via dir + explicitly) is audited once
                seen.add(rp)
                files.append(f)
    return files


def dotenv_key_shadow_report(paths):
    """STATIC scan: keys sitting UNCOMMENTED in a repo's .env-family files that WOULD shadow the keys.env SSOT once the
    app calls load_dotenv() — the shadow `doctor` cannot catch, because a bare CLI never runs that load_dotenv(), so the
    value is not yet in os.environ. key_shadow_report() reads the RUNTIME os.environ; THIS reads the FILES. A key is
    considered ONLY when its name EXACTLY matches one keys.env declares — so whether a var is a provider key is keys.env's
    OWN declaration (a fact), never guessed from the name (a `_TOKEN`/`_KEY` suffix rule would mis-read an app secret
    like CSRF_TOKEN as a provider key). Per matching (file, key): state 'differ' = the file's value ≠ keys.env's (the
    dangerous active-when-loaded shadow of a possibly-rotated key — exactly the warden 401), 'dup' = identical value
    (redundant, but a key must still live ONLY in keys.env). A commented / profile-suffixed / empty line, and any name
    keys.env does not declare, never flags. Read-only, $0, LAST-4 only in the result (values are compared in memory,
    never printed). `paths` = files and/or directories; a directory is scanned (non-recursive) for the known .env-family
    names. Returns [{path, name, state, file4, declared4}]; never raises per path."""
    declared = {}
    for k, v in _iter_env_file(KEYS_ENV):
        if k and v and "__" not in k:
            declared.setdefault(k, v)                     # first wins, matching load_key_files order
    out = []
    for f in _dotenv_scan_files(paths):
        for k, v in _iter_env_file(f):
            if "__" in k or not v:
                continue                                  # a profile entry (__ = the fixed suffix convention) / empty value: no bare shadow
            d = declared.get(k)                           # keys.env's declared NAMES decide what counts as a key here — a fact, not a name guess
            if d is None:
                continue                                  # a name keys.env does not declare cannot shadow the SSOT — not this audit's concern
            state, declared4 = ("dup", d[-4:]) if v == d else ("differ", d[-4:])
            out.append({"path": str(f), "name": k, "state": state, "file4": v[-4:], "declared4": declared4})
    return out


def key_source(name):
    """Where the ACTIVE value of provider key `name` actually comes from — 'keys.env', 'profile:<p>', 'external'
    (a shadow), or 'missing'. Determined by COMPARING the resolved value to the declared default and the active
    profile's entry — NOT by set-membership (_KEYS_SET_BY_SPENDGUARD holds BOTH default-set and profile-set vars, so
    membership can't tell them apart). Read-only, $0."""
    active = os.environ.get(name) or api_key(name)
    if not active:
        return "missing"
    default = prof_val = None
    prof = _key_profile()
    for k, v in _iter_env_file(KEYS_ENV):
        if k == name and default is None:
            default = v
        elif prof and k == name + "__" + prof:
            prof_val = v
    if prof and prof_val is not None and active == prof_val:
        return "profile:" + prof
    if default is not None and active != default:
        return "external"                                # differs from keys.env's default AND not a profile match
    return "keys.env"


def provider_key_status():
    """Per provider key (openai/anthropic) for `doctor`: ALL determination happens HERE (validated) so the display
    just renders a returned state — no inference in the print loop. Returns [{prov, name, resolved4, state, ...}],
    state ∈ {'missing','shadowed','ok'}: 'shadowed' = an external env/.env value overrides keys.env (a stale one is
    how a rotated key keeps 401ing) and carries `declared4`; 'ok' carries `source` ∈ {'keys.env','profile:<p>'}.
    Read-only, $0, last-4 only (never the secret)."""
    shadows = {s["name"]: s for s in (key_shadow_report() or [])}
    out = []
    for prov, name in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
        k = api_key(name)
        if not k:
            out.append({"prov": prov, "name": name, "resolved4": "", "state": "missing"})
        elif name in shadows:
            out.append({"prov": prov, "name": name, "resolved4": k[-4:], "state": "shadowed",
                        "declared4": shadows[name]["declared4"]})
        else:
            out.append({"prov": prov, "name": name, "resolved4": k[-4:], "state": "ok", "source": key_source(name)})
    return out


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


def _provider_key_env(provider):
    """Return the environment variable holding this provider's API key."""
    return resolve_provider_key_env(provider)


def key_fingerprint(provider):
    """Short non-secret fingerprint of the key currently serving `provider` — `<sha256[:8]>:<last4>` — stamped
    on every charge so per-key spend is attributable/reconcilable (which workspace/project key did this?). The
    env-resolved key is a PROXY for the key the consumer's client used (clients overwhelmingly read the env;
    an explicit api_key= differing from env would be mis-fingerprinted — documented limitation). last4 matches
    what provider dashboards display. '' when the provider has no known key env or no key set. LOCAL-ONLY:
    the roll-up push never selects this column."""
    var = _provider_key_env(provider)
    key = os.environ.get(var, "") if var else ""
    if not key:
        return ""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:8] + ":" + key[-4:]








def api_key(name):
    """Resolve an API key: os.environ first, then a CHAIN of .env files — $SPENDGUARD_ENV, ./.env (cwd), and
    SPENDGUARD_HOME/.env. The last is cwd-INDEPENDENT, so keys resolve from any repo (if spendguard lives outside a
    consumer repo, a cwd-only ./.env silently loses the keys — financial data must not depend on which directory you ran from)."""
    k = os.environ.get(name, "")
    if k:
        return k
    candidates = []
    if os.getenv("SPENDGUARD_ENV"):
        candidates.append(Path(os.getenv("SPENDGUARD_ENV")))
    candidates.append(Path.cwd() / ".env")
    candidates.append(HOME / "keys.env")       # the scaffolded secrets file (primary) — found from any directory
    candidates.append(HOME / ".env")           # legacy stable-home .env (still honored for existing installs)
    for envp in candidates:
        try:
            if envp.exists():
                for ln in envp.read_text().splitlines():
                    s = ln.strip()
                    if s.startswith(name + "="):
                        return s.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


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


# Load the key files at IMPORT — but at the END of the module (not mid-file): profile resolution reads
# saas_config()/_project_saas above, which must be defined first. The module body completes before any
# importer's code runs, so keys are still in the environment before any provider client is constructed.
load_key_files()




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
