"""THE single, data-driven registry of every $0 subscription lane — one row per lane, the source everything reads.

Adding a lane is ONE entry here (+ its `<name>_exec.py` module). adapters routing (`_LANES`), lanes.py activation /
probe / creds / auth, and lane_catalog reasoning ALL DERIVE from this — no module enumerates lanes independently.
(Learned adding the Kimi lane: a scattered lane set meant one new lane broke six tests and five source sites; this
table makes a lane DATA, not code — the user's "config in one place" rule.)

Each row (a plain dict — pure data, no imports of adapters/lanes so this stays a leaf both can import):
  lane        — the lane / executor name (e.g. 'kimi-code'); what the ledger records as `executor`.
  provider    — the metered PROVIDER it stands in for (adapters.PROVIDERS key); the routing + pricing key.
  exec        — the `<name>_exec` module that drives it (imported lazily BY NAME, never at module load).
  kind        — 'cli' (a host binary + a login artifact) or 'key' (an HTTP endpoint + a plan key).
  creds       — for a CLI lane, the login-artifact path(s) that prove login; () for a key lane.
  keychain    — optional macOS keychain service whose mere presence is INCONCLUSIVE ('unknown', not 'ok').
  probe_tier  — the model tier `spendguard lanes --probe` pings it with; None = the lane's own default model.
  reasoning   — how the lane expresses reasoning effort (style / levels / default) — the per-lane CLI/API quirk.
  login       — the exact activation step init / doctor / lanes print when the lane is not ready.

(Which lanes are SESSION-mined for plan value is NOT here — that truth lives once in the miner registry
receipt._SOURCE_REFRESH, and lane_value derives 'ledger-valued' as this lane set minus that; duplicating it would
be the very drift this table exists to end.) A test isolates artifacts by repointing a row's `creds` at a temp path
— lane_registry.lane_spec(name)["creds"] = (tmp,) — so no suite reads the host's real login.
"""
from pathlib import Path


def _home_path(*parts):
    return Path.home().joinpath(*parts)


LANES = [
    {"lane": "claude-code", "provider": "anthropic", "exec": "subscription_exec", "kind": "cli",
     "creds": (_home_path(".claude", ".credentials.json"),), "keychain": "Claude Code-credentials", "probe_tier": "haiku",
     "reasoning": {"style": "thinking", "levels": (), "default": None},
     "login": ("run `claude` then `/login`, sign in with your SUBSCRIPTION account — and if it offers to use a "
               "detected ANTHROPIC_API_KEY, choose No: Yes meters every call to the API instead of your plan")},
    {"lane": "codex", "provider": "openai", "exec": "codex_exec", "kind": "cli",
     "creds": (_home_path(".codex", "auth.json"),), "keychain": None, "probe_tier": None,
     "reasoning": {"style": "param", "levels": ("none", "low", "medium", "high", "xhigh", "max"), "default": None},
     "login": "run `codex` and sign in with your ChatGPT account (not an API key)"},
    {"lane": "gemini", "provider": "gemini", "exec": "antigravity_exec", "kind": "cli",
     # agy's OAuth path DRIFTS across versions — accept ANY known artifact (current + legacy) so a renamed token
     # never reports a logged-in lane as inactive.
     "creds": (_home_path(".gemini", "antigravity-cli", "antigravity-oauth-token"),
               _home_path(".gemini", "oauth_creds.json")),
     "keychain": None, "probe_tier": None,
     "reasoning": {"style": "suffix", "levels": ("low", "medium", "high"), "default": "medium"},
     "login": ("install the Antigravity CLI (`curl -fsSL https://antigravity.google/cli/install.sh | bash`), then "
               "run `agy` and sign in with your Google account — decline any API-key option (a key meters every "
               "call to the Gemini API instead of your Antigravity plan)")},
    {"lane": "zai-coding", "provider": "zai", "exec": "zai_exec", "kind": "key",
     "creds": (), "keychain": None, "probe_tier": None,
     "reasoning": {"style": "none", "levels": (), "default": None},
     "login": ("add a z.ai GLM Coding Plan key to keys.env — `ZAI_CODING_API_KEY` (or your account's `ZAI_API_KEY` "
               "on an active plan); this lane is a key + endpoint, not a CLI login")},
    {"lane": "kimi-code", "provider": "moonshot", "exec": "kimi_exec", "kind": "cli",
     "creds": (_home_path(".kimi-code", "credentials"),), "keychain": None, "probe_tier": None,
     "reasoning": {"style": "none", "levels": (), "default": None},
     "login": ("run `kimi` then `/login` and choose Kimi Code OAuth (your subscription) — NOT the Moonshot API "
               "key, which meters every call to the API instead of your plan")},
]

_BY_LANE = {r["lane"]: r for r in LANES}


def all_lanes():
    """Every lane name, sorted — the ONE list to iterate (never a hardcoded set)."""
    return sorted(_BY_LANE)


def lane_spec(lane):
    """The full row for a lane, or None. (Returns the live dict so a test can repoint its `creds`.)"""
    return _BY_LANE.get(lane)


def provider_lane_map():
    """{provider: (lane, exec_module_name)} — the shape adapters._LANES needs, derived from this table."""
    return {r["provider"]: (r["lane"], r["exec"]) for r in LANES}


def probe_tiers():
    """{lane: probe_tier} — the tier `spendguard lanes --probe` pings each lane with (None = its own default)."""
    return {r["lane"]: r["probe_tier"] for r in LANES}


def reasoning_quirk(lane):
    """How a lane expresses reasoning effort (style / levels / default), or None if unregistered."""
    r = _BY_LANE.get(lane)
    return dict(r["reasoning"]) if r else None


# Guards (loud, at import): one lane per provider — the routing invariant adapters._LANES relies on (a duplicate
# provider would silently drop a lane from the derived map), and unique lane names.
assert len({r["provider"] for r in LANES}) == len(LANES), "lane_registry: two lanes share a provider"
assert len(_BY_LANE) == len(LANES), "lane_registry: two rows share a lane name"
