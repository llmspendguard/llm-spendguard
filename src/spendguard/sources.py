"""`spendguard sources` — where CAN this machine spend, and what do we already see?

ONE discovery, three signals, no interrogation. A first-time user should never have to know which tools we
support in order to tell us what they use — so nothing here asks. It looks:

  1. **providers you pay** — resolved keys (config.api_key over keys.env/env). The cheapest, most reliable
     signal, and the one that points at REAL BILLED dollars.
  2. **agent tools on disk** — Claude Code, Codex, and any adapter registered through the port below. Plan-
     covered work that never shows on an invoice but is very much spend.
  3. **interpreters that can spend** — `coverage.audit()` already finds venvs with an LLM SDK installed and
     reports whether they're gated. Ungated ones are where money escapes.

DELIBERATE BOUNDARY: we never read the user's SOURCE CODE. Grepping repos for `import openai` is far more
invasive than checking which packages are installed, and it answers worse — installed SDKs + which interpreters
exist tells you exactly where spend can originate without reading a line of anyone's code. For a tool asking to
be trusted with money and transcripts, that distinction is the whole argument.

Everything here is deterministic, local, free, and LLM-free. Finding files is mechanical; only MEANING (which
project/org a session belongs to) is agentic, and that lives elsewhere and is opt-in.

## The transcript-source PORT

Adding a tool should not mean editing spendguard. A source is an object/module with:

    NAME     = "aider"                  # display name
    def detect() -> bool                # cheap: does this tool exist on this machine?
    def read() -> {"sessions": int, "days": [str], "total_usd": float,
                   "projects": {name: usd}, "models": {name: usd}}

Register a built-in with `register(name, factory)`; third parties ship one from a `spendguard.providers`
entry point (the same group the GPU adapters use) and ride `scan` / `sources` with zero special-casing.
Fail-open per adapter, exactly like gpu_port: a broken source warns once and is skipped — it can never break
the others or the command.
"""
import sys

_SOURCES = {}          # name → zero-arg factory returning a source object/module
_BUILTINS_DONE = False


def register(name, factory):
    """Add a transcript source. `factory` is zero-arg and returns the source (see the port contract above)."""
    _SOURCES[name] = factory


def _register_builtins():
    global _BUILTINS_DONE
    if _BUILTINS_DONE:
        return
    _BUILTINS_DONE = True

    def _mk(mod_name, display):
        def _factory():
            import importlib
            mod = importlib.import_module(f".{mod_name}", __package__)
            return _LedgerSource(display, mod)
        return _factory

    register("claude-code", _mk("claudecode", "Claude Code"))
    register("codex", _mk("codex", "Codex"))
    # third-party sources: same entry-point group the GPU adapters use.
    try:
        from . import provider_plugins
        provider_plugins.load()
    except Exception:
        pass


class _LedgerSource:
    """Adapts the built-in transcript readers (claudecode / codex) to the port. They already parse, dedup and
    accumulate into a per-(project, model, day) ledger — this only shapes their state, it never re-parses."""

    def __init__(self, name, mod):
        self.NAME = name
        self._mod = mod

    def detect(self):
        try:
            import os
            d = self._mod._projects_dir() if hasattr(self._mod, "_projects_dir") else self._mod._sessions_dir()
            return os.path.isdir(d)
        except Exception:
            return False

    def read(self, days=None):
        import datetime
        st, _pass = self._mod.update()
        try:
            self._mod._save_state(st)                     # keep the watermark: the next scan is incremental
        except Exception:
            pass
        cutoff = ((datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None)
        rec = {"sessions": len(st.get("sessions") or {}), "days": set(), "total_usd": 0.0,
               "projects": {}, "models": {}}
        for v in (st.get("ledger") or {}).values():
            if v.get("_work") or (cutoff and (v.get("day") or "") < cutoff):
                continue
            usd = float(v.get("cost") or 0)
            if usd <= 0:
                continue
            rec["total_usd"] += usd
            p = v.get("project") or "(unknown)"
            rec["projects"][p] = rec["projects"].get(p, 0.0) + usd
            if v.get("model"):
                rec["models"][v["model"]] = rec["models"].get(v["model"], 0.0) + usd
            if v.get("day"):
                rec["days"].add(v["day"])
        rec["days"] = sorted(rec["days"])
        return rec


def transcript_sources():
    """[(name, source)] for every registered source that DETECTS on this machine. A source that raises during
    detection is skipped with one warning — never fatal."""
    _register_builtins()
    out = []
    for name, factory in sorted(_SOURCES.items()):
        try:
            src = factory()
            if src.detect():
                out.append((name, src))
        except Exception as e:
            print(f"[spendguard] WARN transcript source {name!r} failed to load (skipped): {e}", file=sys.stderr)
    return out


def providers_paid():
    """[{provider, env, resolved, kind}] — which providers have a resolvable key HERE. `kind` is 'llm' or
    'compute': both are REAL billed dollars and both reconcile, but spendguard keeps the two axes separate
    everywhere else (caps, reporting, the statement), so the discovery view must not blur them either.
    NEVER returns or prints the key itself — only whether one resolves."""
    from . import config, config_schema
    try:
        from . import adapters
        llm_envs = {p["key_env"] for p in adapters.PROVIDERS.values()}
    except Exception:
        llm_envs = set()
    seen, out = set(), []
    for s in config_schema.SETTINGS:
        env = s.get("env") or ""
        if s.get("section") != "keys" or not env.endswith("_API_KEY") or env in seen:
            continue
        seen.add(env)
        try:
            resolved = bool(config.api_key(env))
        except Exception:
            resolved = False
        out.append({"provider": env[: -len("_API_KEY")].lower(), "env": env, "resolved": resolved,
                    "kind": "llm" if env in llm_envs else "compute"})
    return out


def discover(days=None):
    """The whole picture, in one call. Free, local, no LLM."""
    tools = []
    for name, src in transcript_sources():
        try:
            rec = src.read(days=days)
        except Exception as e:
            tools.append({"name": getattr(src, "NAME", name), "key": name, "error": str(e)[:100]})
            continue
        rec.update(name=getattr(src, "NAME", name), key=name, error=None)
        tools.append(rec)
    try:
        from . import coverage
        venvs = coverage.audit()
    except Exception:
        venvs = []
    return {"providers": providers_paid(), "tools": tools, "venvs": venvs,
            "ungated": [v for v in venvs if not v.get("gated") and not v.get("own")]}


def render(d):
    paid = [p for p in d["providers"] if p["resolved"]]
    lines = ["spendguard sources — where this machine can spend, and what we already see.",
             "  (local + free: resolved keys, agent tools on disk, interpreters with an LLM SDK. "
             "Your source code is never read.)", ""]

    # LLM and remote compute are kept apart here for the same reason they're apart everywhere else in spendguard:
    # they're both real billed $, but they're different caps, different reconcilers, different lines on the close.
    lines.append("PROVIDERS you pay (real billed $):")
    for kind, label in (("llm", "LLM"), ("compute", "remote compute")):
        got = [p for p in paid if p["kind"] == kind]
        if got:
            lines.append(f"  {label}: " + ", ".join(f"✓ {p['provider']}" for p in got))
    if paid:
        lines.append("    → `spendguard reconcile all` diffs your ledger against these providers' actual bills.")
    else:
        lines.append("    (none resolved — add one to ~/.spendguard/keys.env to reconcile against real billing)")
    missing = [p["provider"] for p in d["providers"] if not p["resolved"]]
    if missing:
        lines.append(f"    not set: {', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}")

    lines += ["", "AGENT TOOLS on this machine (plan-covered work — value, not billed $):"]
    if d["tools"]:
        for t in d["tools"]:
            if t.get("error"):
                lines.append(f"    ! {t['name']:<14} unreadable: {t['error']}")
            else:
                span = f"{t['days'][0]} → {t['days'][-1]}" if t["days"] else "no dated activity"
                lines.append(f"    ✓ {t['name']:<14} {t['sessions']:>5} sessions · {span} · "
                             f"est ${t['total_usd']:,.2f}")
        lines.append("    → `spendguard scan` breaks this down by project.")
    else:
        lines.append("    (none found — Claude Code and Codex are built in; other tools plug in via the")
        lines.append("     `spendguard.providers` entry point, see docs/PROVIDERS.md)")

    lines += ["", "INTERPRETERS that can spend:"]
    if d["venvs"]:
        gated = [v for v in d["venvs"] if v.get("gated") or v.get("own")]
        lines.append(f"    {len(gated)} gated · {len(d['ungated'])} NOT gated")
        for v in d["ungated"][:5]:
            lines.append(f"    ⚠ ungated: {v['venv']}  ({'+'.join(v['llm'])})")
        if d["ungated"]:
            lines.append("    → spend from these never reaches the gate. Fix one:")
            lines.append(f"      spendguard install-hook --venv {d['ungated'][0]['venv']}")
            lines.append("      …or run jobs through `spendguard run -- <cmd>` (nothing installed).")
    else:
        lines.append("    (no venvs with an LLM SDK found near here — set coverage.roots if yours live elsewhere)")
    return "\n".join(lines)


def main(argv=None):
    argv = list(argv or [])
    days = None
    for i, a in enumerate(argv):
        if a == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                print("--days takes a number, e.g. --days 30")
                return 2
    if "--json" in argv:
        import json
        d = discover(days=days)
        d["providers"] = [{k: v for k, v in p.items()} for p in d["providers"]]   # keys are never included
        print(json.dumps(d, indent=2, default=str))
        return 0
    print(render(discover(days=days)))
    return 0
