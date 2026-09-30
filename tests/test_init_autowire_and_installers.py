"""Guards for `spendguard init` auto-setup (items 1 + 2):

  PART 1 — LANE AUTO-WIRE (setup._autowire_lanes). A READY $0 lane + an UNSET executor ⇒ advisor.executor is written
  'pool' (realtime calls prefer the plan at $0). An executor the user ALREADY chose (stored) or the env pins is LEFT
  untouched. No ready lane ⇒ NO write (safe on CI / a bare host). advisor.lane_models is NEVER auto-seeded — a priced
  GUESS would undercount the lane's value (a wrong number pointing the wrong way); the user declares it deliberately
  via `spendguard lanes set-model`, which validates the model is priced.

  PART 2 — INTEGRATION INSTALLERS (setup._run_integration_installers). Plain --quick stays CONFIG-ONLY (no installer
  runs — backward-compatible); --all --quick runs every installer once (one-command full setup); a GOVERNANCE stop
  (SpendGateRefused / ledger.LockedError) from any installer PROPAGATES, never downgraded to 'skipped'.

  PART 3 — helpers: _prompt_yn (assume / EOF→eof / empty→default) and _reraise_if_governance_stop (re-raises a stop or
  a lock, passes an ordinary exception through).

  PART 4 — KEY PRE-FLIGHT (setup._preflight_keys, item 3). Data-driven over EVERY declared secret key (not a
  hand-picked openai/anthropic pair); the non-secret key_profile SELECTOR is excluded; every secret key has a
  get-a-key link in config_schema.KEY_HELP_URLS (a new key added without one fails this test); a missing key prints
  its URL.

  PART 5 — CLOSING CARD (setup._setup_summary_card, item 4). Reflects the effective executor + the ready lanes + what
  was wired, offers the $0 end-to-end proof, and survives None/empty inputs.

Offline, no network, no spend, isolated SPENDGUARD_HOME. Lane detection AND the installers are monkeypatched, so the
test asserts the WIRING DECISIONS — never a live host lane state or a real ~/.claude / venv write.
"""
import contextlib
import io
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-init-autowire-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
# Offline convention (see tests/test_anthropic_vision_timeout.py): supply our OWN fake keys so nothing short-circuits
# on keyless CI. This test never reaches a provider (all paths are monkeypatched) but the convention is cheap + safe.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-offline")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-offline")
# A dev shell may export this; pin it OFF so PART 1's "env pins the executor" case starts from a clean baseline.
os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import setup, config, config_schema, tier_config, gate, ledger   # noqa: E402
from spendguard import lanes as _lanes                                # noqa: E402


def _row(lane, provider, auth, cli_present=True, activate=None):
    """One lanes_status row shaped like lanes.lanes_status() emits (lane/provider/enabled/cli/auth/activate)."""
    return dict(lane=lane, provider=provider, enabled=False,
                cli=(f"/usr/local/bin/{lane}" if cli_present else None), auth=auth, activate=activate)


def _status(rows, executor="api"):
    return {"executor": executor, "lanes": rows}


def _reset_cfg():
    """Wipe the isolated config.json between cases so each starts from 'executor unset'."""
    try:
        config.CONFIG_JSON.unlink()
    except FileNotFoundError:
        pass
    config.cfg_invalidate()


def main():
    fails = []

    def ck(name, cond):
        print(("  [OK] " if cond else "  [FAIL] ") + name)
        if not cond:
            fails.append(name)

    # keep PART 1 output quiet + deterministic — the DECISION (what got written) is what we assert, not the printout
    _lanes.lane_summary_lines = lambda: []

    # ── PART 1: lane auto-wire ──
    print("-- PART 1: _autowire_lanes writes executor='pool' ONLY when a lane is READY and the executor is unset --")

    # (a) ready lane + unset executor -> pool, and lane_models stays empty
    _reset_cfg()
    _lanes.lanes_status = lambda: _status([_row("codex", "openai", "ok")])
    setup._autowire_lanes()
    ck("ready lane + unset executor -> advisor.executor := 'pool'",
       config._cfg_get("advisor", "executor", None) == "pool")
    ck("lane_models NOT auto-seeded (no priced guess that would undercount value)",
       not (config._cfg_get("advisor", "lane_models", {}) or {}))

    # (b) ready lane but executor ALREADY chosen (stored) -> left as-is (never clobbered)
    _reset_cfg()
    tier_config._write_advisor_cfg("executor", "codex")
    setup._autowire_lanes()
    ck("stored executor 'codex' left untouched",
       config._cfg_get("advisor", "executor", None) == "codex")

    # (c) ready lane but env pins the executor -> nothing persisted (env wins at runtime; don't write over it)
    _reset_cfg()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    setup._autowire_lanes()
    ck("env-pinned executor -> nothing persisted (stored stays unset)",
       config._cfg_get("advisor", "executor", None) is None)
    os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)

    # (d) NO ready lane -> no write (a bare host / CI stays on the metered API)
    _reset_cfg()
    _lanes.lanes_status = lambda: _status([_row("codex", "openai", "missing", cli_present=False,
                                                activate="log in: codex login")])
    setup._autowire_lanes()
    ck("no ready lane -> advisor.executor NOT written",
       config._cfg_get("advisor", "executor", None) is None)

    # ── PART 2: integration installers ──
    print("\n-- PART 2: _run_integration_installers — --quick config-only; --all --quick runs all; stops propagate --")
    from spendguard import mcp_server, receipt
    seen = []
    setup.install_hook = lambda **kw: seen.append("hook")
    mcp_server.register_client = lambda *a, **k: seen.append("mcp")
    receipt.install_cli = lambda a: seen.append("receipt")
    setup.install_rule = lambda *a, **k: seen.append("rule")
    setup.install_skills = lambda *a, **k: seen.append("skills")

    seen.clear()
    setup._run_integration_installers(quick=True, do_all=False)
    ck("plain --quick runs NO installer (config-only, backward-compatible)", seen == [])

    seen.clear()
    setup._run_integration_installers(quick=True, do_all=True)
    ck("--all --quick runs all five installers once",
       sorted(seen) == ["hook", "mcp", "receipt", "rule", "skills"])

    # plain interactive: a single GATEWAY question gates the whole flow. Stub _prompt_yn to force the answer.
    _real_prompt = setup._prompt_yn
    seen.clear()
    setup._prompt_yn = lambda *a, **k: False               # gateway declined
    setup._run_integration_installers(quick=False, do_all=False)
    ck("interactive gateway DECLINED -> no installer runs", seen == [])
    seen.clear()
    setup._prompt_yn = lambda *a, **k: True                # gateway + every per-step accepted
    setup._run_integration_installers(quick=False, do_all=False)
    ck("interactive gateway ACCEPTED -> all five installers run",
       sorted(seen) == ["hook", "mcp", "receipt", "rule", "skills"])
    setup._prompt_yn = _real_prompt

    def _boom(**kw):
        raise gate.SpendGateRefused("cap exceeded")
    setup.install_hook = _boom
    raised = False
    try:
        setup._run_integration_installers(quick=True, do_all=True)
    except gate.SpendGateRefused:
        raised = True
    ck("a SpendGateRefused from an installer PROPAGATES (never downgraded to 'skipped')", raised)

    # ── PART 3: helpers ──
    print("\n-- PART 3: _prompt_yn + _reraise_if_governance_stop --")
    ck("_prompt_yn(assume=True) returns default True", setup._prompt_yn("x", default=True, assume=True) is True)
    ck("_prompt_yn(assume=True, default=False) returns False", setup._prompt_yn("x", default=False, assume=True) is False)

    import builtins
    _orig = builtins.input
    builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())
    try:
        ck("_prompt_yn EOF returns the eof arg (gateway: False on a piped/CI init)",
           setup._prompt_yn("x", default=True, eof=False) is False)
        ck("_prompt_yn EOF returns default when eof unset", setup._prompt_yn("x", default=True) is True)
    finally:
        builtins.input = _orig

    ck("_reraise_if_governance_stop passes an ordinary exception through (no raise)",
       setup._reraise_if_governance_stop(ValueError("ordinary")) is None)
    reraised = False
    try:
        setup._reraise_if_governance_stop(gate.SpendGateRefused("x"))
    except gate.SpendGateRefused:
        reraised = True
    ck("_reraise_if_governance_stop re-raises SpendGateRefused", reraised)
    locked = False
    try:
        setup._reraise_if_governance_stop(ledger.LockedError("x"))
    except ledger.LockedError:
        locked = True
    ck("_reraise_if_governance_stop re-raises ledger.LockedError", locked)

    # ── PART 4: key pre-flight (item 3) — data-driven over ALL secret keys, each with a get-a-key link ──
    print("\n-- PART 4: _preflight_keys covers EVERY declared secret key + a link for each; excludes the selector --")
    secret_keys = [s["env"] for s in config_schema.SETTINGS
                   if s["section"] == "keys" and s.get("secret") and s.get("env")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resolved, missing = setup._preflight_keys()
    out = buf.getvalue()
    ck("_preflight_keys returns counts covering EVERY declared secret key (data-driven, not a hand-picked pair)",
       resolved + missing == len(secret_keys) and len(secret_keys) >= 8)
    ck("openai + anthropic (our fake keys) resolve", resolved >= 2)
    ck("the non-secret key_profile SELECTOR is excluded (not a credential)", "SPENDGUARD_KEY_PROFILE" not in out)
    ck("EVERY declared secret key has a get-a-key link (a new key without one fails here)",
       all(env in config_schema.KEY_HELP_URLS for env in secret_keys))
    ck("a missing key prints its get-a-key URL", "get one: http" in out)

    # ── PART 5: the closing setup card (item 4) — reflects executor + ready lanes, offers the $0 proof ──
    print("\n-- PART 5: _setup_summary_card reflects the executor + ready lanes and offers the $0 proof --")
    card = io.StringIO()
    with contextlib.redirect_stdout(card):
        setup._setup_summary_card((2, 10),
                                  {"executor": "pool", "lanes": [_row("codex", "openai", "ok")]},
                                  ["gate", "MCP"])
    ctext = card.getvalue()
    ck("card shows the effective executor (pool)", "advisor.executor = pool" in ctext)
    ck("card lists the ready lane + what was wired", "codex" in ctext and "gate" in ctext and "MCP" in ctext)
    ck("card offers the $0 end-to-end proof (lanes --probe)", "lanes --probe" in ctext)
    ok_none = True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            setup._setup_summary_card(None, None, None)   # nothing ready / nothing wired — must not crash
    except Exception:
        ok_none = False
    ck("card handles None/empty inputs without crashing", ok_none)

    print(f"\n{'[FAIL]' if fails else 'OK'} test_init_autowire_and_installers: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
