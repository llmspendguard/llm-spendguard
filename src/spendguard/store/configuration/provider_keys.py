"""Provider-key loading, resolution, auditing, and attribution."""

import os
from pathlib import Path

from spendguard.store.configuration.config_store import HOME, saas_config
from spendguard.store.configuration.provider_key_env_registry import all_provider_key_env_items
from spendguard.store.configuration.provider_key_env_registry import resolve_provider_key_env


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
    """Load the declared secret files (~/.spendguard/keys.env, then $SPENDGUARD_ENV) into
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
    for p in (KEYS_ENV, *( [Path(os.environ["SPENDGUARD_ENV"])] if os.environ.get("SPENDGUARD_ENV") else [] )):
        for k, v in _iter_env_file(p):
            if k and v and k not in entries:             # first declared file wins per variable
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
    for prov, name in sorted(all_provider_key_env_items().items()):
        k = api_key(name)
        if not k:
            out.append({"prov": prov, "name": name, "resolved4": "", "state": "missing"})
        elif name in shadows:
            out.append({"prov": prov, "name": name, "resolved4": k[-4:], "state": "shadowed",
                        "declared4": shadows[name]["declared4"]})
        else:
            out.append({"prov": prov, "name": name, "resolved4": k[-4:], "state": "ok", "source": key_source(name)})
    return out


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
    """Resolve an API key from the process environment or declared key SSOT files only."""
    k = os.environ.get(name, "")
    if k:
        return k
    candidates = []
    if os.getenv("SPENDGUARD_ENV"):
        candidates.append(Path(os.getenv("SPENDGUARD_ENV")))
    candidates.append(HOME / "keys.env")       # the scaffolded secrets file (primary) — found from any directory
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


# Load the key files at IMPORT — but at the END of the module (not mid-file): profile resolution reads
# saas_config()/_project_saas above, which must be defined first. The module body completes before any
# importer's code runs, so keys are still in the environment before any provider client is constructed.
load_key_files()
