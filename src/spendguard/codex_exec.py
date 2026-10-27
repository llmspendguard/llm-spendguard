"""Codex subscription lane — run spendguard's OPENAI-model meta prompts on the ChatGPT plan (Pro/Plus),
mirroring subscription_exec (the Anthropic/Max lane) over the Codex CLI.

  • `codex exec <prompt> --json --output-last-message <file>` — one non-interactive run: the final
    agent message lands in the file (the text contract), the JSONL event stream on stdout carries
    token usage when the CLI version emits it;
  • OPENAI_API_KEY is STRIPPED from the child env — the CLI runs on the ChatGPT plan login, so the
    call can never silently become a metered API charge (exactly the claude-code lane's guarantee);
  • accounting stays two-axis: $0 BILLED (kind='subscription', executor 'codex'); plan VALUE is
    counted by the existing codex est-value pipeline from the session logs;
  • any failure — CLI missing, timeout, non-zero exit, empty output, plan window exhausted — returns
    {error} and the caller falls back (pool → API): the lane can degrade, the advisor cannot break.

LIVE-VERIFY PENDING: the Codex CLI is not installed on the dev machine, so this lane is verified
structurally (offline stubs encode the documented `codex exec` interface); the defensive parse +
fallback mean an interface mismatch degrades to the API path rather than erroring. Usage extraction
matches FIELD NAMES anywhere in the event stream (mechanical extraction, tolerant of event-schema
drift across CLI versions) — absent usage records 0 tokens, never a guess.

TWO PATHS, TWO USAGE CONTRACTS (so the "never a guess" line is not read too broadly): the EXEC path
(`codex exec --json`) carries real usage in the event stream → measured, or 0 when absent, never guessed. The
WARM-DAEMON path (`codex mcp-server`) returns the answer TEXT but NO token usage, so it ESTIMATES tokens from
character length (len//4) for the est-value axis ONLY — an explicit proxy, marked at the call site, never a
measured count. $0 billed either way (a flat-fee plan served it).

Doctrine note: prompt-mode ONLY, same as the claude-code lane — no tools, no agent loop for meta work.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import logging
import threading
import contextlib
from .execution_journal import _protect_durable_execution_io

CODEX_MCP_SCAN_INTERVAL_S = 0.05
CODEX_MCP_EXIT_WAIT_S = 1.0
CODEX_ORPHAN_PARENT_PID = 1
CODEX_CONFIG_FILENAME = "config.toml"
CODEX_DEFAULT_HOME = "~/.codex"
CODEX_MCP_DEFAULT_URL_PORTS = {"http": 80, "https": 443}
CODEX_APP_SERVER_RATE_LIMITS_METHOD = "account/rateLimits/read"
CODEX_EXEC_WORK_DIRECTORY_PREFIX = "spendguard-codex-"
CODEX_USAGE_UNKNOWN_SAMPLE_COUNT = 0
CODEX_DELEGATE_OUTPUT_FILENAME = "delegate-final.txt"
CODEX_CONFIG_OVERRIDE_FLAG = "--config"
CODEX_DELEGATE_READ_ONLY_APPROVAL_MODE = "auto"
CODEX_MCP_TOOL_APPROVAL_OVERRIDE_TEMPLATE = 'mcp_servers.{server}.tools.{tool}.approval_mode="{mode}"'
CODEX_DELEGATE_REQUIRED_MCP_READ_ONLY_TOOLS = {
    "symgrep": ("search", "indexed_roots"),
    "7thsense": ("find", "ask", "index_queue_status", "profile"),
    "honestreview": ("warm_daemon_metadata",),
    "spendguard": (),
}
CODEX_DELEGATE_REQUIRED_MCP_SERVERS = tuple(CODEX_DELEGATE_REQUIRED_MCP_READ_ONLY_TOOLS)


def _codex_stdio_reap_policy(env, cwd=None):
    """Read transport boundaries from the same config home as the child. Fail closed."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    from urllib.parse import urlparse
    home = env.get("CODEX_HOME") or os.path.expanduser(CODEX_DEFAULT_HOME)
    with open(os.path.join(home, CODEX_CONFIG_FILENAME), "rb") as stream:
        servers = tomllib.load(stream).get("mcp_servers", {})
    signatures, ports = [], set()
    launch_cwd = cwd or os.getcwd()
    search_path = os.pathsep.join(os.path.join(launch_cwd, entry) if not os.path.isabs(entry) else entry
                                 for entry in env.get("PATH", os.defpath).split(os.pathsep))
    for server in servers.values():
        if server.get("url"):
            url = urlparse(server["url"])
            ports.add(url.port or CODEX_MCP_DEFAULT_URL_PORTS[url.scheme])
        elif server.get("command"):
            command = server["command"]
            if os.path.dirname(command) and not os.path.isabs(command):
                command = os.path.join(launch_cwd, command)
            executable = shutil.which(command, path=search_path) or command
            signatures.append((os.path.realpath(executable), tuple(server.get("args", []))))
    return signatures, ports


def _captured_codex_stdio_is_reapable(proc, birth, policy):
    """Mechanical identity, exact configured transport signature, sockets and parent guard."""
    import psutil
    signatures, ports = policy
    argv = proc.cmdline()
    if proc.create_time() != birth or not argv:
        return False
    if not any(os.path.realpath(argv[0]) == exe and tuple(argv[1:]) == args
               for exe, args in signatures):
        return False
    sockets = proc.net_connections(kind="inet")
    if any(conn.status == psutil.CONN_LISTEN or
           (conn.laddr and conn.laddr.port in ports) for conn in sockets):
        return False
    return proc.ppid() == CODEX_ORPHAN_PARENT_PID and proc.create_time() == birth


def _reap_captured_codex_stdio(captured, policy):
    """Only signal identity-checked, captured STDIO descendants now adopted by init.

    A live parent of ANY kind is protected, stronger than checking Codex names.
    Socket inspection must succeed; all listeners are protected, including URL services.
    psutil's terminate additionally guards against PID reuse at signal time.
    """
    import psutil
    reaped = 0
    for pid, birth in captured.items():
        try:
            proc = psutil.Process(pid)
            if not _captured_codex_stdio_is_reapable(proc, birth, policy):
                continue
            proc.terminate()
            try:
                proc.wait(timeout=CODEX_MCP_EXIT_WAIT_S)
            except psutil.TimeoutExpired:
                # A TERM-resistant server still gets EVERY safety check before escalation.
                if not _captured_codex_stdio_is_reapable(proc, birth, policy):
                    continue
                proc.kill()
                proc.wait(timeout=CODEX_MCP_EXIT_WAIT_S)
            reaped += 1
        except psutil.Error:
            continue
    logging.getLogger(__name__).info("cold codex MCP cleanup: captured=%d reaped=%d", len(captured), reaped)
    return reaped


@_protect_durable_execution_io
def _run_cold_codex_with_reaper(cmd, timeout, env, cwd):
    """Observe only this Popen's descendants; cleanup cannot alter the run result.

    Polling never guesses attribution: descendants missed before reparenting are
    deliberately left alone. There is no global signature-based sweep.
    """
    import psutil
    from . import execution_journal
    dispatch_id = execution_journal.current_durable_execution_id()
    captured = {}
    stop = threading.Event()
    logger = logging.getLogger(__name__)
    try:
        policy = _codex_stdio_reap_policy(env, cwd=cwd)
    except Exception:
        logger.warning("cold codex MCP cleanup: config unavailable; cleanup disabled", exc_info=True)
        policy = ([], set())
    with contextlib.ExitStack() as evidence_streams:
        paths = execution_journal.durable_execution_payload_paths(dispatch_id) if dispatch_id else None
        stdout_target = evidence_streams.enter_context(paths["stdout"].open("ab")) if paths else subprocess.PIPE
        stderr_target = evidence_streams.enter_context(paths["stderr"].open("ab")) if paths else subprocess.PIPE
        child = evidence_streams.enter_context(subprocess.Popen(cmd, stdout=stdout_target, stderr=stderr_target, text=True,
                          env=env, stdin=subprocess.DEVNULL, cwd=cwd))
        if dispatch_id:
            execution_journal.bind_durable_execution_child(dispatch_id, child.pid)
        def _capture_cold_codex_descendants(root):
            try:
                while not stop.is_set():
                    for descendant in root.children(recursive=True):
                        try:
                            captured[descendant.pid] = descendant.create_time()
                        except psutil.Error:
                            continue
                    stop.wait(CODEX_MCP_SCAN_INTERVAL_S)
            except Exception:
                logger.warning("cold codex MCP descendant capture stopped", exc_info=True)

        tracker = None
        try:
            # Construct before communicate can reap the root, preventing PID-reuse attribution.
            root = psutil.Process(child.pid)
            tracker = threading.Thread(target=_capture_cold_codex_descendants, args=(root,), daemon=True)
            tracker.start()
        except Exception:
            tracker = None
            logger.warning("cold codex MCP capture unavailable", exc_info=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
            if paths:
                for evidence in (stdout_target, stderr_target):
                    evidence.flush()
                    os.fsync(evidence.fileno())
                execution_journal.persist_durable_execution_payload(dispatch_id, exit_code=child.returncode)
                stdout = paths["stdout"].read_text(encoding="utf-8")
                stderr = paths["stderr"].read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            child.kill()
            # Don't drain inherited pipes before cleanup: an orphan may hold stderr open.
            child.wait()
            if dispatch_id:
                execution_journal.persist_durable_execution_payload(dispatch_id, exit_code=child.returncode)
            raise
        finally:
            try:
                stop.set()
                if tracker is not None:
                    tracker.join()
                if child.poll() is not None:
                    _reap_captured_codex_stdio(captured, policy)
            except Exception:
                logger.warning("cold codex MCP cleanup failed", exc_info=True)

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report
CODEX_EXEC_SUBCOMMAND = "exec"
CODEX_SANDBOX_FLAG = "--sandbox"
CODEX_AGENTIC_APPROVAL_FLAG = "--approve-for-me"
_USAGE_TTL_S = 300            # the codex rate-limit log is re-read at most this often (shared cache adds reset-boundary)
_AUTH_RECHECK_BACKOFF_S = 0.75  # a non-zero `codex login status` is confirmed once (transient vs real logout) before False
_TOKEN_EXPIRY_MARGIN_S = 120    # a token within this of its own exp is not treated as still-valid (leave room to refresh)
_ABSENT_CONFIRM_TRIES = 3       # a momentarily-ABSENT auth.json (non-atomic refresh rewrite) is confirmed over this many
#                                 short reads before it is called a real logout — an absent flicker is NOT 'logged out'


def _codex_delegate_read_only_tool_approval_arguments():
    """Build per-invocation auto-approval overrides solely from the delegated MCP SSOT."""
    arguments = []
    for server, tools in CODEX_DELEGATE_REQUIRED_MCP_READ_ONLY_TOOLS.items():
        for tool in tools:
            override = CODEX_MCP_TOOL_APPROVAL_OVERRIDE_TEMPLATE.format(
                server=server, tool=tool, mode=CODEX_DELEGATE_READ_ONLY_APPROVAL_MODE)
            arguments.extend((CODEX_CONFIG_OVERRIDE_FLAG, override))
    return arguments


def _validate_codex_exec_approval_boundary(command):
    """Reject the Codex CLI flag pair that the CLI itself defines as mutually exclusive."""
    if CODEX_AGENTIC_APPROVAL_FLAG in command and CODEX_SANDBOX_FLAG in command:
        raise ValueError(
            f"codex {CODEX_EXEC_SUBCOMMAND} cannot combine {CODEX_AGENTIC_APPROVAL_FLAG} "
            f"with {CODEX_SANDBOX_FLAG}"
        )
    return command


def _token_unexpired(now=None, margin=None):
    """Authoritative STRUCTURAL check: is the Codex plan token on disk present and not past its own expiry? Reads
    ${CODEX_HOME:-~/.codex}/auth.json and decodes the access_token JWT's `exp` claim — PARSING a fixed-format field
    (the exp integer), never a judgement about prose. Returns True when a token is present AND exp is more than
    `margin` seconds in the future; False when present but within `margin` of (or past) exp; None when it cannot be
    determined (home/file/field absent or unparseable) so a can't-read is NEVER mistaken for a logout.

    `margin` defaults to _TOKEN_EXPIRY_MARGIN_S — headroom for 'is this token safe to RELY ON for a call that may run a
    while'. For the LOGOUT decision pass margin=0: a token whose exp is still in the FUTURE means LOGGED IN (a refresh
    is merely imminent), NOT logged out. The default margin reported a valid near-exp token as False, which fired the
    'codex lane LOGGED OUT' banner in the ~2min window before every hourly token refresh — the recurring false toast.
    The token's own exp is the DEFINITIVE 'am I logged in' signal, beating a `codex login status` subprocess that races
    the refresh. $0, never raises."""
    import base64
    try:
        home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        with open(os.path.join(home, "auth.json")) as fh:
            tok = ((json.load(fh).get("tokens") or {}).get("access_token")) or ""
        parts = tok.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return None
        _m = _TOKEN_EXPIRY_MARGIN_S if margin is None else margin
        return (float(exp) - (now if now is not None else time.time())) > _m
    except Exception:
        return None
_usage_cache = {"at": 0.0, "val": None}


def _bin():
    """Host codex CLI via config.resolve_cli ($SPENDGUARD_CODEX_BIN pin → PATH → well-known user-local
    dirs) — daemons run with a minimal PATH that misses nvm/~/.local installs."""
    # NO shutil.which FAST PATH. It ran BEFORE the pin was consulted, so $SPENDGUARD_CODEX_BIN was silently
    # ignored whenever a `codex` existed on PATH — an explicit pin overridden by whatever was lying around, in
    # a tool whose whole premise is never silently substituting one thing for another. resolve_cli already
    # does the PATH lookup (pin → PATH → well-known dirs), so nothing is lost by delegating outright, and a
    # pin that points at a missing binary now fails LOUD instead of falling through to a different binary.
    from . import config
    return config.resolve_cli("codex", "SPENDGUARD_CODEX_BIN")


def available() -> bool:
    return _bin() is not None


def auth_status(timeout=20):
    """DEFINITIVE plan-login state from the CLI's OWN status command → {"authed": True|False|None}. `codex login
    status` EXITS 0 when signed in and non-zero when not — a STRUCTURAL signal (the process exit code), read
    directly, never a regex on its printed prose. True/False from returncode when the CLI answers; None when it
    can't be determined (CLI absent, timeout) so a can't-check is NEVER mistaken for a positive logout.

    A SINGLE non-zero exit is a TRANSIENT under process contention (many codex spawns racing on the auth/refresh
    file), NOT a logout — so a non-zero is CONFIRMED by one re-check before being reported False: a real logout
    stays non-zero on both attempts, a transient recovers on the second. Without this, one flapping status call
    fired a persistent 'logged out (re-login)' banner while the 10-day token was in fact valid, and the banner then
    re-notified every 30 min until a successful call cleared it. Run on the plan LOGIN env (metered keys stripped)
    so it reflects the auth the lane's run_prompt uses. $0. Never raises."""
    try:
        exe = _bin()                               # INSIDE the try: _bin() "fails LOUD" (raises) on a pinned
        if not exe:                                # SPENDGUARD_CODEX_BIN that points at a missing binary — that must
            return {"authed": None}                # become an honest {"authed": None}, never propagate (docstring: Never raises)
        from . import config
        env = config.lane_exec_env(exe)
        r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode == 0:
            return {"authed": True}
        # A non-zero `codex login status` is NOT authoritative — it races the periodic OAuth token refresh that
        # rewrites ~/.codex/auth.json. The TOKEN on disk decides: a VALID token is authed (the hourly false-toast fix),
        # a present-and-EXPIRED token is a real logout. margin=0: the LOGOUT decision uses the token's ACTUAL exp — a
        # token still valid but within the refresh margin is LOGGED IN (a refresh is imminent), not logged out. Using
        # the default margin here is what fired the banner in the ~2min window before every hourly refresh.
        tok = _token_unexpired(margin=0)
        if tok is True:
            return {"authed": True}
        if tok is False:
            return {"authed": False}
        # Token UNREADABLE (a non-atomic auth.json rewrite in flight, or process contention): ONE confirming re-check —
        # a transient non-zero status recovers to 0 here (the legitimate recovery path).
        time.sleep(_AUTH_RECHECK_BACKOFF_S)
        r2 = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=timeout, env=env)
        if r2.returncode == 0:
            return {"authed": True}
        # BOTH the status AND the re-check are non-zero. The token file may be MID-REFRESH: the codex CLI rewrites
        # ~/.codex/auth.json on its periodic token refresh, and under heavy concurrent codex use (many lane calls /
        # delegations at once) that rewrite window is hit often, leaving auth.json momentarily UNREADABLE *or* ABSENT
        # (a non-atomic unlink+write). NEITHER is a confirmed logout — the earlier fix caught 'unreadable' but an
        # ABSENT flicker still escalated to False and fired the toast (measured 2026-10-09: hourly + under-load false
        # 'codex lane LOGGED OUT' while the token was valid for ~8 more days). CONFIRM over a short bounded window: a
        # VALID token that (re)appears → authed; a PRESENT + genuinely EXPIRED token → real logout; a file that stays
        # ABSENT across the WHOLE window → real logout (a user `codex logout`). A file that merely flickered
        # absent/unreadable mid-rewrite → INCONCLUSIVE (None), which never fires the 'logged out' toast.
        import os as _os
        _home = _os.environ.get("CODEX_HOME") or _os.path.expanduser("~/.codex")
        _path = _os.path.join(_home, "auth.json")
        _ever_present = False
        for _ in range(_ABSENT_CONFIRM_TRIES):
            t = _token_unexpired(margin=0)
            if t is True:
                return {"authed": True}                   # a valid token (re)appeared → authed (the refresh completed)
            if t is False:
                return {"authed": False}                  # present + genuinely expired → real logout
            try:
                if _os.path.exists(_path):
                    _ever_present = True                  # there but unreadable this instant → a rewrite in flight
            except Exception:
                _ever_present = True
            time.sleep(_AUTH_RECHECK_BACKOFF_S)
        # Across the whole confirm window: never a readable token. ABSENT every check → confirmed logout; otherwise the
        # file was present-but-unreadable (a refresh rewrite) → inconclusive (None), never a false toast.
        return {"authed": False if not _ever_present else None}
    except Exception:
        return {"authed": None}


def _usage_from_events(stdout):
    """Best-effort (input_tokens, output_tokens) from the --json event stream: scan every JSON line for
    the usage field names wherever they appear, keep the LARGEST seen (events report cumulative totals).
    Mechanical extraction only — absent/unparseable usage is (0, 0), never invented."""
    in_tok = out_tok = 0

    def _scan(d):
        nonlocal in_tok, out_tok
        if isinstance(d, list):                       # usage can sit inside ARRAYS too (a choices/items/events list) —
            for it in d:                               # the contract is "field names ANYWHERE in the stream", so descend
                _scan(it)                              # into lists as well as dicts, else array-nested usage reads 0.
            return
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if isinstance(v, (dict, list)):
                _scan(v)
            elif k == "input_tokens" and isinstance(v, (int, float)):
                in_tok = max(in_tok, int(v))
            elif k == "output_tokens" and isinstance(v, (int, float)):
                out_tok = max(out_tok, int(v))
    for ln in (stdout or "").splitlines():
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            _scan(json.loads(s))
        except Exception:
            continue
    return in_tok, out_tok


def _plugin_disable_flags():
    """`codex exec` COLD-STARTS every call, and the dominant cost is loading the user's enabled plugins / MCP servers
    (MEASURED 2026-08-19: a real one-shot went from >75s to **5s** with them off). A headless completion needs none
    of them, so disable each ENABLED plugin for THIS invocation — read live from ~/.codex/config.toml so it adapts to
    whatever the user has (no hardcoded plugin names). NB: a single `-c 'plugins={}'` does NOT work — the per-plugin
    `[plugins."x@y"]` tables win — so each must be turned off by name. Best-effort → [] on any parse problem."""
    cfg = os.path.expanduser("~/.codex/config.toml")
    try:
        import tomllib
        with open(cfg, "rb") as f:
            d = tomllib.load(f)
    except Exception:
        return []
    flags = []
    for name, rec in (d.get("plugins") or {}).items():
        if isinstance(rec, dict) and rec.get("enabled"):
            flags += ["-c", f'plugins."{name}".enabled=false']
    return flags


def _mcp_disable_flags():
    """Disable MCP orchestration for a headless, one-shot `codex exec` — the OTHER half of the cold-start load the
    plugin disable above addresses. Read-only comprehension does not need the user's configured mcp_servers,
    while workspace-write agentic runs retain them for tools such as symgrep. MCP-enabled codex sessions
    CONNECT to shared URL services and SPAWN configured STDIO command servers. The app-server does not reap
    those command children when the session ends — measured 2026-10-11:
    at --workers 4 × ~53s/call a long delegation run abandoned ~12 full 7-server sets/min (~1.5 GB/min, hundreds of
    orphaned procs). Codex 0.160.1 gates configured servers on ONE global key; the global gate is used, NOT per-entry
    `mcp_servers."x".enabled=false` overrides — a config layer can replace a per-entry's required transport fields and
    make the app-server exit before the initialize handshake (the warm-daemon path proved this). The warm daemon
    (codex_daemon) also applies this; cold read-only runs use it to avoid the same leak."""
    return ["-c", "orchestrator.mcp.enabled=false"]


def _daemon_enabled():
    """Use the WARM `codex mcp-server` (codex_daemon) instead of cold-starting `codex exec` per call? Env
    SPENDGUARD_CODEX_DAEMON wins, else config advisor.codex_daemon. Default ON so agentic delegation pays the daemon
    startup once and subsequent turns are warm. Disabling it refuses execution: cold processes cannot honor
    the broker's global worker cap."""
    from . import config
    v = os.getenv("SPENDGUARD_CODEX_DAEMON")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(config._cfg_get("advisor", "codex_daemon", True))


def _codex_effort(level):
    """Map a STANDARD ordinal reasoning level → the Codex plan model's OWN scale. The Codex model accepts
    none|low|medium|high|xhigh|max and has NO 'minimal' (that is the OpenAI *API* scale — MEASURED 2026-08-19:
    sending 'minimal' is a hard 400 "not supported with the ... model. Supported values are: none, low, medium,
    high, xhigh, max"). So 'minimal' → 'none' (its floor); the rest pass through and codex validates them (a bad
    value fails fast → the lane degrades to API). Empty → None (leave codex's own default)."""
    lv = (level or "").strip().lower()
    if not lv:
        return None
    return "none" if lv == "minimal" else lv


# ── codex QUOTA: authenticated app-server control surface (no model turn, no metered call) ──────────────
def _codex_home():
    """Codex's state dir: $CODEX_HOME, else the documented default ~/.codex. Not hardcoded to one path — an override
    or a relocated home still resolves (and _plugin_disable_flags already reads config.toml from the same home)."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def gc_shell_snapshots(max_age_days=7, apply=False):
    """Prune codex CLI's shell-snapshot residue. codex writes ONE .sh per `codex exec` into
    <codex_home>/shell_snapshots, and spendguard's codex lane drives that heavily — measured 2026-09-15, 7.2GB /
    38,739 files. A snapshot restores an interactive shell's environment; spendguard's one-shot `codex exec` calls
    never reuse theirs, and no active session touches one older than a day, so pruning by AGE is safe. DRY-RUN by
    default (reports what it WOULD remove); deletes only with apply=True; NEVER touches a recent (possibly in-use)
    snapshot. It deletes in codex's OWN dir, so it is opt-in: the caller (the CLI `spendguard codex-gc`, dry-run
    first) or a scheduled job runs it — spendguard never prunes it silently on the hot path.

    Honest reporting (never masks a failure as a clean sweep): a directory listing that fails sets `error`; a
    per-file stat/unlink failure is COUNTED in `skipped`, so `deleted` vs `skipped` is truthful. Returns a summary
    dict {dir, examined, stale, bytes, deleted, skipped, apply, cutoff_days, error}."""
    out = {"dir": None, "examined": 0, "stale": 0, "bytes": 0, "deleted": 0, "skipped": 0, "apply": bool(apply),
           "cutoff_days": max_age_days, "error": None}
    d = os.path.join(_codex_home(), "shell_snapshots")
    out["dir"] = d
    if not os.path.isdir(d):
        return out                                        # no dir → genuinely nothing to do (error stays None)
    try:
        names = os.listdir(d)
    except OSError as e:                                  # a real scan failure is surfaced, not a false 'empty, clean'
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return out
    cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
    for name in names:
        p = os.path.join(d, name)
        try:
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
        except OSError:
            out["skipped"] += 1                           # could not stat → COUNTED, never silently ignored
            continue
        out["examined"] += 1
        if st.st_mtime >= cutoff:
            continue                                      # recent — an active session may still hold it; leave it
        out["stale"] += 1
        out["bytes"] += int(st.st_size)
        if apply:
            try:
                os.unlink(p)
                out["deleted"] += 1
            except OSError:
                out["skipped"] += 1                       # stale but could not delete (perms/locked) → COUNTED
    return out


def _parse_rate_limits(obj):
    """Parse the installed app-server GetAccountRateLimitsResponse schema into quota buckets."""
    response = obj or {}
    snapshots = response.get("rateLimitsByLimitId")
    snapshot_rows = (list(snapshots.items()) if isinstance(snapshots, dict) and snapshots
                     else [(None, response.get("rateLimits"))])
    out = []
    for limit_id, snapshot in snapshot_rows:
        if not isinstance(snapshot, dict):
            continue
        label = snapshot.get("limitName") or snapshot.get("limitId") or limit_id
        for window_name in ("primary", "secondary"):
            window = snapshot.get(window_name)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            used_percent = float(window["usedPercent"])
            if not 0 <= used_percent <= 100:
                continue
            duration = window.get("windowDurationMins")
            bucket_prefix = str(label) if label is not None else "default"
            bucket_suffix = f" ({int(duration)}m window)" if duration is not None else ""
            out.append({"bucket": f"{bucket_prefix}:{window_name}{bucket_suffix}",
                        "remaining_pct": max(0, 100 - int(round(used_percent))),
                        "reset_ts": (float(window["resetsAt"]) if window.get("resetsAt") is not None else None)})
    return out or None


def _codex_usage_report_uncached():
    """Query the authenticated running Codex app-server and return a complete headroom report."""
    report = {"buckets": None, "source": CODEX_APP_SERVER_RATE_LIMITS_METHOD, "reason": None,
              "n": CODEX_USAGE_UNKNOWN_SAMPLE_COUNT, "window": None, "uncertainty": "provider_data_unknown"}
    try:
        from . import codex_daemon
        response = codex_daemon.read_rate_limits(timeout=codex_daemon.RATE_LIMITS_READ_TIMEOUT_S)
        if not isinstance(response, dict) or response.get("error"):
            report["reason"] = "codex_warm_pool_rate_limits_unavailable"
            return report
        payload = response.get("rate_limits")
        if not isinstance(payload, dict):
            report["reason"] = "codex_warm_pool_rate_limits_response_malformed"
            return report
        report["buckets"] = _parse_rate_limits(payload)
        if not report["buckets"]:
            report["reason"] = "codex_rate_limits_windows_missing"
            return report
        report["uncertainty"] = ("provider_reported_reset_unknown" if any(
            bucket["reset_ts"] is None for bucket in report["buckets"]) else "provider_reported")
    except Exception:
        report["reason"] = "codex_warm_pool_rate_limits_request_failed"
    return report


def codex_headroom_report():
    """Cached structured report from Codex's authenticated account/rateLimits/read control method."""
    from . import subscription_headroom_surfaces as surfaces
    return surfaces.cached_subscription_surface_report(_usage_cache, _USAGE_TTL_S, _codex_usage_report_uncached)


HEADROOM_REPORT_READER = codex_headroom_report


def usage():
    """Codex plan quota buckets, or None when the control surface cannot establish headroom."""
    return codex_headroom_report()["buckets"]


def _codex_error_status(err_text):
    """The HTTP status code codex reported in a failed turn's error payload, as an int, or None when absent. codex's
    app-server wraps the provider error as JSON carrying a numeric `"status"` (MEASURED 2026-10-07: a model the plan
    does not serve returns `{"type":"error","status":400,"error":{"type":"invalid_request_error", ...}}`). This pulls
    that known-shape integer out — FIELD PARSING on a documented payload, not a judgement about the message's meaning —
    so the shared vendor_call._classify status taxonomy (400/401/403/413/414 → PAYLOAD_REJECTED, NON-retryable) applies
    to a codex lane error exactly as to a metered 4xx: a deterministic bad request (e.g. an unsupported model) is not
    retried, while a transient transport failure carries NO status and stays correctly retryable. None on no match."""
    import re as _re
    m = _re.search(r'"status"\s*:\s*(\d{3})', err_text or "")   # the documented numeric field, not the prose message
    return int(m.group(1)) if m else None


def _codex_structured_http_status(payload):
    """Read the provider's protocol status fields without interpreting error prose."""
    if not isinstance(payload, dict):
        return None
    for field in ("httpStatusCode", "status"):
        value = payload.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    for value in payload.values():
        status = _codex_structured_http_status(value)
        if status is not None:
            return status
    return None


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, max_tokens=None,
               sandbox="read-only", cwd=None, recycle_on_timeout=None, delegate_context=False):
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Codex run. `system` is
    prepended to the prompt (codex exec has no separate system slot for one-shot prompt mode). `model` IS
    forwarded to `codex -m` when given (e.g. gpt-5.5), so the recorded model is the one that actually ran —
    and an id the plan does not serve (MEASURED: gpt-5.6 is rejected 400 "not supported ... with a ChatGPT
    account") makes codex exit non-zero, so adapters cools the lane and falls back to the metered API: the
    lane degrades, it never silently answers on a different model than the caller asked for.
    `--skip-git-repo-check` because a headless one-shot is not an interactive session that needs the
    working-tree guard, and honestreview may run it from any directory (measured: /tmp tripped the guard).
    `sandbox` defaults to the historical read-only behavior; callers deliberately delegating an agentic repository
    task may pass workspace-write. Any other value is refused before the CLI is touched."""
    if sandbox not in ("read-only", "workspace-write"):
        return {"error": f"unsupported codex sandbox {sandbox!r}"}
    if sandbox == "workspace-write" and cwd is None:
        return {"error": "workspace-write codex execution requires an explicit trusted cwd (target repo path)"}
    neutral_cwd = None
    try:
        if sandbox == "read-only":
            neutral_cwd = tempfile.mkdtemp(prefix=CODEX_EXEC_WORK_DIRECTORY_PREFIX)
            resolved_cwd = neutral_cwd
        else:
            resolved_cwd = os.path.abspath(os.path.expanduser(os.fspath(cwd)))
        return _run_prompt_in_cwd(prompt, system=system, model=model, timeout=timeout, reasoning=reasoning,
                                  max_tokens=max_tokens, sandbox=sandbox, cwd=resolved_cwd,
                                  recycle_on_timeout=recycle_on_timeout, delegate_context=delegate_context)
    finally:
        if neutral_cwd:
            shutil.rmtree(neutral_cwd, ignore_errors=True)


def _run_prompt_in_cwd(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, max_tokens=None,
                       sandbox="read-only", cwd=None, recycle_on_timeout=None, delegate_context=False):
    """Execute after ``run_prompt`` has resolved and, when needed, owned the task working directory."""
    _eff = _codex_effort(reasoning)   # the effort ACTUALLY applied on this lane (Codex's own scale: 'minimal'→'none').
    #                                   Reported on the result so the ledger records what RAN, not the requested tier —
    #                                   letting a caller VERIFY the lane matched its metered fallback's applied reasoning.
    # Agentic delegation needs the user's configured MCP envelope. The warm app-server deliberately disables MCP,
    # so this one path uses documented headless approval review and leaves configured servers enabled.
    if sandbox == "workspace-write" and delegate_context:
        return _run_agentic_codex_exec(prompt, system=system, model=model, timeout=timeout,
                                       reasoning=reasoning, cwd=cwd)
    # The broker is the sole admission point for read-only/meta work. Failure must never start an uncounted cold process.
    if _daemon_enabled():
        from . import codex_daemon
        _full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
        _t0 = time.time()
        try:
            _r = codex_daemon.run_warm(_full, model=model, reasoning=reasoning, sandbox=sandbox, cwd=cwd,
                                       timeout=timeout, recycle_on_timeout=recycle_on_timeout)
        except Exception as _e:
            _r = {"error": f"codex daemon raised: {_e}"}
        if _r.get("text") and not _r.get("error"):
            if sandbox == "workspace-write" and not (
                    _r.get("thread") and _r.get("turn") and _r.get("process_pid")):
                return {"error": "codex execution start unconfirmed: broker supplied no thread/turn/process evidence"}
            _txt = _r["text"]
            return {"text": _txt, "in_tok": len(_full) // 4, "out_tok": len(_txt) // 4,   # est: the tool returns no usage
                    "execution_started": bool(_r.get("thread") and _r.get("turn") and _r.get("process_pid")),
                    "thread": _r.get("thread"), "turn": _r.get("turn"), "process_pid": _r.get("process_pid"),
                    "latency": round(time.time() - _t0, 2), "effort": _eff, "error": None}
        _derr = _r.get("error") or "codex warm admission returned no final message"
        structured_status = _codex_structured_http_status(_r.get("provider_error"))
        return {"error": _derr, "status_code": structured_status if structured_status is not None
                else _codex_error_status(_derr)}
    return {"error": "codex broker admission disabled; uncounted cold execution refused"}


def _run_agentic_codex_exec(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, cwd=None):
    """Run the delegated workspace task through cold ``codex exec`` with MCP and reviewed approvals enabled."""
    executable = _bin()
    if not executable:
        return {"error": "codex CLI unavailable for delegated workspace execution"}
    from . import config
    environment = config.lane_exec_env(executable)
    from . import execution_journal
    dispatch_id = execution_journal.current_durable_execution_id()
    if dispatch_id is None:
        dispatch_id = execution_journal.begin_durable_execution({"provider": "codex", "cwd": os.fspath(cwd)})
        with execution_journal.durable_execution_scope(dispatch_id):
            try:
                result = _run_agentic_codex_exec(prompt, system=system, model=model, timeout=timeout,
                                                reasoning=reasoning, cwd=cwd)
                execution_journal.terminalize_durable_execution(
                    dispatch_id, "failure" if result.get("error") else "success", envelope=result)
                return result
            except BaseException:
                execution_journal.terminalize_durable_execution(dispatch_id, "failure")
                raise
    complete_prompt = f"{system.strip()}\n\n{prompt}" if system else prompt
    started_at = time.time()
    with tempfile.TemporaryDirectory(prefix=CODEX_EXEC_WORK_DIRECTORY_PREFIX) as output_directory:
        output_path = os.fspath(execution_journal.durable_execution_payload_paths(dispatch_id)["result"])
        # codex-cli >=0.160: `--approve-for-me` runs in (and implies) the workspace-write sandbox and REJECTS a
        # co-passed `--sandbox` ("cannot be used with '--approve-for-me'"). Older codex accepted the explicit flag
        # redundantly; the approval flag alone is the version-robust spelling of "agentic workspace-write + auto-review".
        command = [
            executable, CODEX_EXEC_SUBCOMMAND, "--json", "--output-last-message", output_path,
            CODEX_AGENTIC_APPROVAL_FLAG,
            "--cd", os.fspath(cwd), "--skip-git-repo-check",
        ]
        command.extend(_codex_delegate_read_only_tool_approval_arguments())
        if model:
            command.extend(["--model", model])
        effort = _codex_effort(reasoning)
        if effort:
            command.extend([CODEX_CONFIG_OVERRIDE_FLAG, f'model_reasoning_effort="{effort}"'])
        _validate_codex_exec_approval_boundary(command)
        try:
            completed = _run_cold_codex_with_reaper(command + [complete_prompt], timeout, environment, cwd)
        except subprocess.TimeoutExpired:
            return {"error": f"codex delegated execution timed out after {timeout} seconds",
                    "execution_started": True}
        if completed.returncode != 0:
            return {"error": completed.stderr.strip() or f"codex delegated execution exited {completed.returncode}",
                    "execution_started": True}
        try:
            with open(output_path, encoding="utf-8") as output_stream:
                answer = output_stream.read()
                os.fsync(output_stream.fileno())
        except OSError as error:
            return {"error": f"codex delegated execution produced no final-message file: {error}",
                    "execution_started": True}
        if not answer:
            return {"error": "codex delegated execution produced an empty final message", "execution_started": True}
        input_tokens, output_tokens = _usage_from_events(completed.stdout)
        return {"text": answer, "in_tok": input_tokens, "out_tok": output_tokens,
                "execution_started": True, "transport": "codex-exec-mcp",
                "approval_mode": CODEX_AGENTIC_APPROVAL_FLAG,
                "required_mcp_servers": list(CODEX_DELEGATE_REQUIRED_MCP_SERVERS),
                "latency": round(time.time() - started_at, 2), "effort": effort, "error": None}
