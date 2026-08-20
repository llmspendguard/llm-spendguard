"""Central config for spendguard — paths, knobs, key loading.

Decoupled from any host repo so the package is portable. Data (gate log, kill-switch
flag, reconcile cache) lives under SPENDGUARD_HOME (default ~/.spendguard). API keys
resolve from the environment first, then SPENDGUARD_ENV or ./.env.
"""
import pathlib as _pathlib
import datetime
import os
from pathlib import Path

HOME = Path(os.getenv("SPENDGUARD_HOME") or (Path.home() / ".spendguard"))
try:
    HOME.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

FLAG = HOME / "disabled"                      # persistent kill switch (touch to disable)
LOG = HOME / "gate_log.jsonl"                 # batch-gate audit trail
ANTHROPIC_CACHE = HOME / "anthropic_usage_cache.json"
RT_LOG = HOME / "realtime_log.jsonl"          # real-time spend log (per-day-per-model rollup)


CONFIG_JSON = HOME / "config.json"             # operational (non-secret) config: caps, budget, emit
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


# Well-known user-local CLI install dirs, for hosts where PATH doesn't carry them (launchd/cron daemons run
# with a minimal PATH that misses ~/.local/bin and nvm's versioned bins — the subscription lanes must still
# find the plan CLIs there). Globs allowed; newest executable wins.
_CLI_SEARCH_DIRS = ("~/.claude/local", "~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin",
                    "~/.nvm/versions/node/*/bin")


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


# Provider → key env var, for the ledger's key fingerprint. Local-only, no hardcoded keys.
#
# THE PROVIDER TABLE ALREADY CARRIES THIS. adapters.PROVIDERS holds a key_env for every vendor including the
# ones missing here — moonshot, zai, deepseek, dashscope — so their charges were stamped with an EMPTY
# fingerprint while the table that knew the answer sat one import away. Same shape as four other things
# found today: the fact exists, and the consumer has its own shorter copy.
#
# The literals below remain as the FLOOR, so a fingerprint still works if adapters cannot be imported.
_PROVIDER_KEY_ENV_FALLBACK = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                              "gemini": "GEMINI_API_KEY"}


def _provider_key_env(provider):
    """The env var holding this provider's key, from the provider registry, falling back to the floor above."""
    p = (provider or "").lower()
    try:
        from . import adapters
        spec = adapters.PROVIDERS.get(p) or {}
        if spec.get("key_env"):
            return spec["key_env"]
    except Exception:
        pass
    return _PROVIDER_KEY_ENV_FALLBACK.get(p)


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


def month_start_utc():
    """First day of the CURRENT month in UTC, as 'YYYY-MM-DD' — the default `since` for every money window.

    Every ledger day-key is written in UTC (budget.record, gate, ledger, receipt, report all use
    datetime.now(timezone.utc)), but the default windows used to be built from `date.today()` — LOCAL time. West
    of UTC that makes the month boundary wrong for 7-8 hours around the 1st: `trust`, `close` and the leak check
    computed a residual that CHANGED depending on what time of day you ran them, and then silently self-corrected
    — the hardest possible bug to chase in an accounting tool. One helper, used everywhere, kills the class."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-01")


def today_utc():
    """Today in UTC, as 'YYYY-MM-DD' — the same reason as month_start_utc()."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def update_json(path, mutate, reason="", keep_backups=None, required=False, quarantine_unparseable=False):
    """The ONE way this package rewrites a whole JSON file. Every such write goes through here.

    A SWEEP ON 2026-08-10 FOUND 29 WHOLE-FILE JSON WRITES ACROSS 20 MODULES, exactly ONE of them atomic and
    five of them carrying the destructive read:

        try:    data = json.load(open(p))
        except: data = {}                 # <- an unreadable file becomes an EMPTY one
        ...
        p.write_text(json.dumps(data))    # <- and is then replaced by it

    That is not a style problem. It is how ~/.spendguard/config.json went from 9KB of settings to a 26-byte
    probe value with nothing raised and nothing logged, and the same shape sits in the caps registry, the
    receipt cache and the lane state. Each site was individually plausible; the pattern was the defect.

    WHAT THIS GUARANTEES
      * an existing file that will not PARSE is never silently replaced — `required=True` raises,
        otherwise the caller is told and the file is left alone
      * the write is ATOMIC (temp file in the same directory + os.replace), so a crash or a full disk
        leaves the previous file whole rather than truncated
      * EMACS-STYLE BACKUP ON EVERY WRITE, not on request. `path~` always holds the immediately-previous
        version, and `keep_backups` timestamped copies hold deeper history (default from config
        `safety.keep_backups`, 3). This used to default to ZERO — the capability existed and almost every
        caller left it off, which is why "every destructive mutation backs up first" came back ABSENT from
        a whole-repo invariant check even after this function was written. A safety default that has to be
        remembered is one that will be forgotten; that is the same failure that lost config.json in the
        first place, one level up. Pass keep_backups=0 to keep only `path~`.

    `mutate(data)` edits in place or returns a new object. Returns the written object, or None if the
    write was declined."""
    import json as _json
    import os as _os
    import datetime as _dt
    path = _pathlib.Path(path)

    cur = {}
    if path.exists():
        try:
            cur = _json.loads(path.read_text() or "{}")
        except Exception as e:
            msg = (f"{path} exists but does not parse ({type(e).__name__}: {str(e)[:60]}) — REFUSING to "
                   f"rewrite it, because doing so would replace its whole contents with this one change.")
            if quarantine_unparseable:
                # SETTINGS AND CACHES NEED OPPOSITE ANSWERS HERE, and only the caller knows which it holds.
                # Refusing forever is right for config.json — those settings exist nowhere else, so a
                # corrupt file must be repaired by a human, not overwritten. It is wrong for an adapter's
                # state file, which is REBUILDABLE from the transcripts: refusing there wedges the adapter
                # permanently, every later save failing on damage that will never repair itself. So the
                # damaged file is MOVED ASIDE (kept, for forensics) and a fresh one written. Nothing is
                # destroyed either way; the difference is whether recovery is automatic.
                import sys as _sys
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                bad = path.with_suffix(path.suffix + f".corrupt.{stamp}")
                try:
                    _os.replace(path, bad)
                    _sys.stderr.write(f"[spendguard] WARN {path} did not parse — moved to {bad.name} and "
                                      f"rebuilding. Nothing was deleted.\n")
                except Exception:
                    _sys.stderr.write(f"[spendguard] WARN {msg}\n")
                    return None
                cur = {}
            else:
                if required:
                    raise ValueError(msg)
                import sys as _sys
                _sys.stderr.write(f"[spendguard] WARN {msg}\n")
                return None

    out = mutate(cur)
    if out is None:
        out = cur

    path.parent.mkdir(parents=True, exist_ok=True)
    # `path~` — the previous version, ALWAYS, before anything replaces it. Cheap, bounded (one copy), and
    # it is the file you actually want at 2am: not a timestamped archive to hunt through, just "what it was
    # a moment ago", exactly where Emacs puts it.
    if path.exists():
        try:
            _pathlib.Path(str(path) + "~").write_bytes(path.read_bytes())
        except Exception:
            pass                                  # a failed backup must not block a legitimate write
    if keep_backups is None:
        keep_backups = keep_backups_default()
    if keep_backups and path.exists():
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in (reason or "save"))[:32]
        try:
            (path.parent / f"{path.stem}.{stamp}.{tag}{path.suffix}").write_text(path.read_text())
            for old in sorted(path.parent.glob(f"{path.stem}.2*{path.suffix}"))[:-keep_backups]:
                old.unlink(missing_ok=True)
        except Exception:
            pass                                  # a failed backup must not block a legitimate write
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_json.dumps(out, indent=2))
    _os.replace(tmp, path)                        # atomic: readers never see a half-written file
    return out


def save_config(mutate, reason=""):
    """The ONLY safe way to change config.json. Read-modify-write, but with the three things the three
    hand-rolled copies of this were missing.

    WHAT WENT WRONG WITHOUT IT (2026-08-10). setup.py, chat.py x2 and pricing.set_price each opened the
    file with `except Exception: cfg = {}` and then rewrote it whole — so ONE unreadable byte replaced
    every setting with whatever key that caller happened to be setting. Separately, a test wrote the path
    directly and reduced ~9KB of settings to 26 bytes. There was no backup, and the loss was silent: the
    tool kept running on schema defaults, `calls.enabled` quietly became False, and the next review wave's
    independent ledger cross-check read $0.00 as a consequence.

    THE THREE GUARANTEES
      1. AN UNREADABLE FILE IS NEVER OVERWRITTEN. If it exists and will not parse, this raises. Refusing to
         save one setting is recoverable; replacing every setting is not.
      2. THE WRITE IS ATOMIC. Written to a temp file in the same directory and os.replace()d, so a crash or
         a full disk leaves the previous file intact rather than a truncated one.
      3. THE PREVIOUS VERSION IS KEPT. A rolling set of timestamped backups, because "I set that last week"
         has to be answerable.

    `mutate` receives the parsed dict and modifies it in place (or returns a new one)."""
    import json as _json
    import os as _os
    import datetime as _dt

    cur = {}
    if CONFIG_JSON.exists():
        try:
            cur = _json.loads(CONFIG_JSON.read_text())
        except Exception as e:
            raise ValueError(
                f"{CONFIG_JSON} exists but does not parse ({type(e).__name__}: {str(e)[:80]}). REFUSING to "
                f"write, because saving would replace every setting in it with just this change. Fix or "
                f"move that file first — a copy of the last good version is in {CONFIG_JSON.parent}.")
        if not isinstance(cur, dict):
            raise ValueError(f"{CONFIG_JSON} does not contain a JSON object — refusing to overwrite it.")

    out = mutate(cur)
    if out is None:
        out = cur
    if not isinstance(out, dict):
        raise ValueError("save_config: the mutate function must leave a dict")

    HOME.mkdir(parents=True, exist_ok=True)
    if CONFIG_JSON.exists():                       # keep the version we are about to replace
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in (reason or "save"))[:32]
        try:
            (CONFIG_JSON.parent / f"config.{stamp}.{tag}.json").write_text(CONFIG_JSON.read_text())
            olds = sorted(CONFIG_JSON.parent.glob("config.2*.json"))[:-10]
            for f in olds:
                f.unlink(missing_ok=True)
        except Exception:
            pass                                   # a failed backup must not block a legitimate save
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(out, indent=2))
    _os.replace(tmp, CONFIG_JSON)                  # atomic: never a half-written config
    cfg_invalidate()
    return out


def cfg_invalidate():
    """Drop the config cache. Call after WRITING config.json in-process.

    _cfg() cached on first read and never re-checked, so a `config set` followed by a read in the SAME
    process returned the value from before the write — the CLI wrote the file, reported success, and then
    acted on the old setting. A cache with no invalidation is a fact frozen at whatever time it was first
    needed."""
    _cfg._cache = None
    _cfg._mtime = None


def _cfg():
    """~/.spendguard/config.json (cached, and re-read when the file changes on disk)."""
    import json as _json
    # THE MTIME IS PART OF THE CACHE KEY. Another process (or `config set` in this one) can rewrite the
    # file at any time, and a stale read of a settings file is how a knob appears not to work.
    try:
        _m = CONFIG_JSON.stat().st_mtime if CONFIG_JSON.exists() else None
    except OSError:
        _m = None
    if getattr(_cfg, "_mtime", "unset") != _m:
        _cfg._cache = None
    _cfg._mtime = _m                       # always recorded, so "no file yet" is a state and not an absence
    if getattr(_cfg, "_cache", None) is None:
        c = {}
        try:
            if CONFIG_JSON.exists():
                c = _json.loads(CONFIG_JSON.read_text())
        except Exception:
            pass
        _cfg._cache = c
    return _cfg._cache


def _cfg_get(section, key, default=None):
    return (_cfg().get(section) or {}).get(key, default)


_GITROOT_CACHE = {}


def git_root_project(cwd):
    """Repo name for a cwd = the git-root basename, lowercased (so a session's SUBDIR — lmm/scripts/fanout —
    collapses to the repo, `lmm`, instead of fragmenting into `fanout`). Matches how the gate tags actual-$ charges
    (budget._project). Cached per dir; returns None when cwd isn't inside a git repo (caller falls back to basename)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GITROOT_CACHE:
        return _GITROOT_CACHE[key]
    out = None
    try:
        import subprocess
        root = subprocess.run(["git", "-C", key, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=2).stdout.strip()
        if root:
            out = os.path.basename(root).strip().lower() or None
    except Exception:
        out = None
    _GITROOT_CACHE[key] = out
    return out


def cap():
    """Per-batch hard cap ($). env GATE_CAP → config.json caps.per_batch → 75."""
    v = os.getenv("GATE_CAP")
    return float(v) if v is not None else float(_cfg_get("caps", "per_batch", 75))


def rt_budget():
    """Cumulative real-time cap ($). env GATE_RT_BUDGET → config.json caps.realtime → 50."""
    v = os.getenv("GATE_RT_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "realtime", 50))


def _policy_cap(cls, window):
    """The org/team's SERVER-pushed cap for (cls, window): {"usd", "mode"} or None. Cached in config.json `policy`
    by `spendguard saas sync` (saas.pull_policy). mode = advisory | enforced."""
    try:
        node = ((_cfg().get("policy") or {}).get("caps") or {}).get(cls) or {}
        v = node.get(window)
        if isinstance(v, dict) and v.get("usd") is not None:
            return {"usd": float(v["usd"]), "mode": v.get("mode", "advisory")}
    except Exception:
        pass
    return None


def policy_caps():
    """The full server-pushed policy {caps:{cls:{window:{usd,mode}}}, asof, pulled_at} — for doctor/receipt to
    surface an advisory org suggestion or an enforced ceiling. Empty dict when none pulled."""
    return _cfg().get("policy") or {}


def class_cap(cls, window):
    """Resource-class spend cap ($) — cls in {total, llm, compute}, window in {daily, monthly}. None = off.
    LOCAL order: env GATE_{CLS}_{WINDOW} (e.g. GATE_LLM_DAILY) → nested config caps.{cls}.{window} → (total only)
    legacy flat caps.{window}. Then the SERVER policy (central caps): an ENFORCED org/team cap is a hard ceiling —
    effective = min(local, enforced), applied even with no local cap (local may only TIGHTEN it, never loosen). An
    ADVISORY policy cap is a suggestion only (surfaced by doctor/receipt) — it does NOT change the effective cap
    (partner, not supervisor)."""
    local = None
    env = os.getenv(f"GATE_{cls.upper()}_{window.upper()}")
    if env is not None:
        local = float(env)
    else:
        caps = _cfg().get("caps") or {}
        flat = caps.get(f"{cls}.{window}")                    # how init/config stores it: caps["llm.daily"]
        if flat is not None:
            local = float(flat)
        else:
            node = caps.get(cls)                              # nested form: caps["llm"]["daily"]
            if isinstance(node, dict) and node.get(window) is not None:
                local = float(node[window])
            elif cls == "total" and caps.get(window) is not None:   # legacy flat caps.daily/monthly == total ceiling
                local = float(caps[window])
    pol = _policy_cap(cls, window)
    if pol and pol.get("mode") == "enforced":                 # org-enforced ceiling; local may only tighten it
        return pol["usd"] if local is None else min(local, pol["usd"])
    return local


def daily_cap():    return class_cap("total", "daily")          # back-compat: the total daily ceiling
def monthly_cap():  return class_cap("total", "monthly")


def meta_cap():
    """Separate daily $ cap for spendguard's OWN advisor LLM use (intent spendguard:*). Default $2/day."""
    v = os.getenv("GATE_META_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "meta", 2.0))


def advisor_model():
    """Model for the advisor's REASONING (insight synthesis + `optimize`). Realtime; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_MODEL > config.json advisor.model > default (Opus 4.8)."""
    return os.getenv("SPENDGUARD_ADVISOR_MODEL") or _cfg_get("advisor", "model", "claude-opus-4-8")


def advisor_judge_model():
    """Model for BULK quality reconstruction / judging. Batch API; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_JUDGE_MODEL > config.json advisor.judge_model > default (Haiku 4.5)."""
    return os.getenv("SPENDGUARD_ADVISOR_JUDGE_MODEL") or _cfg_get("advisor", "judge_model", "claude-haiku-4-5")


def recall_model():
    """Model for the AGENTIC RECALL pass (conv.classify_evidence — "is this chunk spend evidence / a cost lesson?").
    A high-volume, simple yes/no classification over the whole corpus, so default to the CHEAPEST capable model
    (gpt-5-nano, $0.05/1M in) — whole-corpus recall lands <10c (~free), which is what lets it replace the keyword
    pre-filters everywhere (incl. the old 'free' index stages) instead of preserving them. Capped by caps.meta.
    Configurable: env SPENDGUARD_RECALL_MODEL > config.json advisor.recall_model > default (gpt-5-nano)."""
    return os.getenv("SPENDGUARD_RECALL_MODEL") or _cfg_get("advisor", "recall_model", "gpt-5-nano")


def validate_advisor():
    """Both advisor models MUST be priced in pricing.py (else the meta estimate/cap can't be computed).
    Returns a list of human-readable problems (empty = OK)."""
    from . import pricing
    problems = []
    for role, m in (("advisor.model", advisor_model()), ("advisor.judge_model", advisor_judge_model())):
        try:
            pricing.price(m)
        except Exception as e:
            problems.append(f"{role}={m!r}: {e}")
    return problems


def budget_backend():
    return _cfg_get("budget", "backend", "memory")


def db_path():
    p = _cfg_get("budget", "db_path", None)
    return p if p else str(HOME / "spend.db")


def ssl_context():
    """SSL context that works under bare venvs too (urllib otherwise can't find CA certs on macOS)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def email_config():
    """SMTP/recipient config from ~/.spendguard/email.json, overlaid by env. Secrets stay here
    (gitignored) or in env — never in the repo."""
    import json as _json
    cfg = {}
    p = HOME / "email.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for key, env in (("host", "SPENDGUARD_SMTP_HOST"), ("port", "SPENDGUARD_SMTP_PORT"),
                     ("user", "SPENDGUARD_SMTP_USER"), ("password", "SPENDGUARD_SMTP_PASS"),
                     ("from_", "SPENDGUARD_EMAIL_FROM"), ("to", "SPENDGUARD_EMAIL_TO"),
                     ("provider", "SPENDGUARD_EMAIL_PROVIDER"), ("api_key", "SPENDGUARD_RESEND_KEY")):
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    return cfg


def _project_saas():
    """Repo-local SaaS overlay: nearest `.spendguard.json` found walking up from CWD (stop at $HOME / fs root).
    Lets DIFFERENT repos on one machine push to different orgs/teams (e.g. nlp-pipeline→Acme/NLP, vision-pipeline→its org).
    Keep it gitignored — it holds the org/team api_key. Overlays the global saas.json; env still wins."""
    import json as _json
    try:
        d = Path.cwd().resolve()
    except Exception:
        return {}
    home = Path.home().resolve()
    for _ in range(40):
        p = d / ".spendguard.json"
        try:
            if p.exists():
                return _json.loads(p.read_text())
        except Exception:
            return {}
        if d == home or d.parent == d:
            break
        d = d.parent
    return {}


def saas_config():
    """SaaS / team roll-up connection. Precedence: global ~/.spendguard/saas.json < repo-local .spendguard.json
    (so each repo can target its own org/team) < env. Secrets (api_key) stay in those gitignored files or env —
    never the repo source. The key is the identity: the server maps it to user/team/org.
    Returns: enabled(bool), url, api_key, visibility, sync_interval, contributor."""
    import json as _json
    cfg = {}
    p = HOME / "saas.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for k, v in _project_saas().items():       # repo-local overlay wins over the global config
        if v is not None and v != "":
            cfg[k] = v
    for key, env in (("enabled", "SPENDGUARD_SAAS"), ("url", "SPENDGUARD_SAAS_URL"),
                     ("api_key", "SPENDGUARD_SAAS_KEY"), ("visibility", "SPENDGUARD_VISIBILITY"),
                     ("sync_interval", "SPENDGUARD_SYNC_INTERVAL"), ("contributor", "SPENDGUARD_CONTRIBUTOR"),
                     ("project", "SPENDGUARD_PROJECT")):
        v = os.environ.get(env)
        if v is not None and v != "":
            cfg[key] = v
    cfg["enabled"] = str(cfg.get("enabled", "")).lower() in ("1", "true", "yes", "y")
    cfg.setdefault("visibility", "private")
    cfg.setdefault("sync_interval", "daily")
    cfg.setdefault("url", "https://llmspendguard.com")   # the only roll-up destination (the hosted aggregator)
    return cfg


def identity_path(): return HOME / "identity.json"


def machine_id():
    """Stable, persisted anonymous contributor id (`usr_<hex>`) for this user/machine — the fallback identity when
    no email is set, so spend is NEVER unattributed and per-user roll-up + billing always have someone to count.
    Generated once, written to ~/.spendguard/identity.json, reused forever. (Replaces the old user@host fallback,
    which leaked the OS username + wasn't a stable id.)"""
    import json as _json
    p = identity_path()
    try:
        if p.exists():
            v = (_json.loads(p.read_text()).get("contributor") or "").strip()
            if v:
                return v
    except Exception:
        pass
    import uuid
    v = "usr_" + uuid.uuid4().hex[:12]
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"contributor": v}))
    except Exception:
        pass
    return v


def is_email(s):
    """True if the contributor string is an email (→ it can double as the alert target). Else it's an anonymous id."""
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (s or "").strip()))


def saas_path(): return HOME / "saas.json"
def saas_state_path(): return HOME / "saas_state.json"   # last_sync timestamp (not the config; written each sync)


def disabled(): return os.getenv("GATE_DISABLE") == "1" or FLAG.exists()
def allow():    return os.getenv("GATE_ALLOW") == "1"


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
    try:
        from . import adapters                          # the registry is the source of truth for per-provider key envs
        metered |= {spec.get("key_env") for spec in adapters.PROVIDERS.values() if spec.get("key_env")}
    except Exception:
        pass
    metered = {m for m in metered if m} - keep
    return {k: v for k, v in os.environ.items() if k not in metered}


# Load the key files at IMPORT — but at the END of the module (not mid-file): profile resolution reads
# saas_config()/_project_saas above, which must be defined first. The module body completes before any
# importer's code runs, so keys are still in the environment before any provider client is constructed.
load_key_files()


def api_get(url, headers, timeout=90):
    """Authenticated GET → parsed JSON. The ONE place an HTTP read to a provider happens.

    There were three: resources._get (timeout, context manager, SSL context — correct), report._paged
    (timeout + context manager, NO SSL context) and reconcile_anthropic._get, which passed NO TIMEOUT and
    returned the raw response object for the caller to json.load. That last one is how a provider stall
    becomes a hung reconcile with no error and no output: urlopen without a timeout waits forever, and the
    socket was never closed either, so a paged walk leaked one connection per page.
    """
    import json as _json
    return _json.loads(api_get_text(url, headers, timeout))


def api_get_text(url, headers, timeout=90):
    """Authenticated GET → decoded body text. Same single transport as api_get; separate because a batch
    RESULTS url serves JSONL, not JSON, and parsing it as JSON would fail on the second line. Two content
    types, one place that opens a socket."""
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read().decode()

import json as _json_mod


# ── per-adapter state files ──────────────────────────────────────────────────────────────────────────────
def keep_backups_default():
    """Timestamped copies retained per rewritten JSON file (config `safety.keep_backups`, env
    SPENDGUARD_KEEP_BACKUPS). The `<file>~` previous-version backup is unconditional and not counted here."""
    import os as _os
    v = _os.environ.get("SPENDGUARD_KEEP_BACKUPS")
    if v is None:
        v = _cfg_get("safety", "keep_backups", 3)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 3


def state_path(name):
    """Path of an adapter's state file under HOME. One naming rule, so `chat`, `claudecode`, `codex`,
    `realized` and the rest cannot drift apart on where they keep it."""
    return HOME / f"{name}_state.json"


def load_state(name, default=None):
    """An adapter's persisted state, or `default` if it is missing OR unreadable. Distinguishing those two
    is the caller's business — save_state refuses to overwrite a file it could not read."""
    try:
        return _json_mod.loads(state_path(name).read_text())
    except Exception:
        return dict(default) if isinstance(default, dict) else (default if default is not None else {})


def save_state(name, obj, loud=True):
    """Persist an adapter's state ATOMICALLY. Returns True on success.

    THERE WERE FIVE COPIES OF THIS, three of them byte-identical:

        try:
            config.HOME.mkdir(parents=True, exist_ok=True)
            _state_path().write_text(json.dumps(st, indent=0))
        except Exception:
            pass

    Both halves are wrong. `write_text` is not atomic, so a crash or a full disk mid-write leaves a
    TRUNCATED state file — and these files are read back with a bare `except: return {}`, so the truncation
    presents as "no state" rather than as damage. And `except: pass` means a save that never happened looks
    exactly like one that did. For claudecode that state holds `counted_ids`, the set that stops resume and
    branch replays from counting the same message.id repeatedly; losing it silently re-inflates est-value by
    roughly 2.4x, and nothing anywhere says so.
    """
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        return update_json(state_path(name), lambda d: obj, reason=f"{name} state",
                           quarantine_unparseable=True) is not None
    except Exception as e:
        if loud:
            import sys as _sys
            _sys.stderr.write(f"  ⚠ could not save {name} state ({type(e).__name__}: {str(e)[:70]}). The next "
                              f"run will re-read from the last saved watermark, which may double-count.\n")
        return False


def uid(n=16):
    """A short random identifier. callio and learn each carried a byte-identical private copy of this."""
    import uuid
    return uuid.uuid4().hex[:n]


_WARNED_ONCE = set()


def warn_once(msg, prefix=""):
    """Print a warning the FIRST time this exact message appears, then never again.

    deid and gate each had their own copy plus their own `_WARNED` set. Two sets means a message crossing
    between them warns twice, and — worse — the reason this pattern exists at all is that an 800-request
    batch warning 800 times is a warning nobody reads. One registry, one line per distinct problem."""
    import sys as _sys
    line = (prefix + msg) if prefix else msg
    if line not in _WARNED_ONCE:
        _WARNED_ONCE.add(line)
        _sys.stderr.write(line + "\n")


def project_of_cwd(cwd, default):
    """Bucket a session cwd to its REPO (git-root basename) so subdirs collapse to the repo and match how
    actual-$ is tagged, instead of fragmenting est-value across dozens of cwd names. `default` is the
    adapter's own fallback label — the ONLY thing that differed between claudecode's and codex's copies."""
    import os as _os
    if not cwd:
        return default
    return git_root_project(cwd) or _os.path.basename(str(cwd).rstrip("/")).lower() or default
