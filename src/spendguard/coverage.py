"""Coverage audit — which python venvs CAN make LLM calls (an openai/anthropic SDK is installed) but are NOT gated
(no spendguard sitecustomize/usercustomize hook). Those are the ungated realtime SOURCES: their spend never hits the
live gate, so it only surfaces (if at all) in after-the-fact reconstruction — the completeness gap. The DURABLE fix is
to gate them at the source (`spendguard install-hook --venv <v>`); this audit makes the gaps visible + prints the fix.

`spendguard coverage` runs it. Roots are configurable (env SPENDGUARD_COVERAGE_ROOTS, colon-separated globs > config
`coverage.roots` > sensible defaults) so it stays portable — no hardcoded user paths.
"""
import os
import glob


# Defaults: the iCloud-safe venv home + venvs sitting inside repos near the cwd (1–2 levels up's siblings). Generic
# globs, not user-specific paths; override with SPENDGUARD_COVERAGE_ROOTS / config coverage.roots.
def _default_roots():
    here = os.getcwd()
    parents = [os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
    roots = ["~/.venvs/*"]
    for p in parents:
        if p and p not in ("/", os.path.expanduser("~")):
            roots.append(os.path.join(p, "*", ".venv*"))
    return roots


def _roots():
    env = os.getenv("SPENDGUARD_COVERAGE_ROOTS")
    if env:
        return [r for r in env.split(":") if r]
    try:
        from . import config
        cfg = config._cfg_get("coverage", "roots", None)
        if cfg:
            return list(cfg) if isinstance(cfg, (list, tuple)) else [cfg]
    except Exception:
        pass
    return _default_roots()


def _is_gated(sp):
    """A venv is gated iff its site-packages carries a sitecustomize/usercustomize that loads spendguard (what
    install-hook writes) — so the gate is active for EVERY interpreter in that venv, even bare scripts."""
    for hook in ("sitecustomize.py", "usercustomize.py"):
        hp = os.path.join(sp, hook)
        try:
            if os.path.exists(hp) and "spendguard" in open(hp, errors="ignore").read():
                return True
        except Exception:
            pass
    return False


def audit(roots=None):
    """[{venv, llm:[providers], gated:bool, own:bool}] for every venv (under roots) that has an LLM SDK installed.
    `own` = it's spendguard's own venv, which self-gates via `import spendguard; spendguard.require()` in its scripts
    (gating it via sitecustomize would spam the receipt on every dev `python` — so that's intentional, not a gap)."""
    pats = roots or _roots()
    seen, out = set(), []
    for pat in pats:
        for venv in glob.glob(os.path.expanduser(pat)):
            for sp in glob.glob(os.path.join(venv, "lib", "python*", "site-packages")):
                if sp in seen:
                    continue
                seen.add(sp)
                llm = [p for p in ("openai", "anthropic") if os.path.isdir(os.path.join(sp, p))]
                if not llm:
                    continue
                out.append({"venv": venv, "llm": llm, "gated": _is_gated(sp),
                            "own": "llm-spendguard" in venv})
    return out


def gaps(roots=None):
    """The ungated LLM venvs that are NOT spendguard's own — the real coverage gaps to gate."""
    return [r for r in audit(roots) if r["llm"] and not r["gated"] and not r["own"]]


def cmd(argv=None):
    rows = audit()
    print("spendguard coverage — LLM-calling venvs (gated = live realtime captured at the source, no reconstruction):")
    if not rows:
        print("  (no venvs with an LLM SDK found under the scanned roots; set SPENDGUARD_COVERAGE_ROOTS to widen)")
        return 0
    g = []
    for r in sorted(rows, key=lambda x: x["venv"]):
        v = r["venv"].replace(os.path.expanduser("~"), "~")
        if r["gated"]:
            tag = "🟢 gated"
        elif r["own"]:
            tag = "🟡 self-gates (spendguard's own venv — scripts call spendguard.require())"
        else:
            tag = "🔴 UNGATED — its realtime spend slips live capture"
            g.append(r)
        print("  %-50s LLM=%-16s %s" % (v, ",".join(r["llm"]), tag))
    if g:
        print("\nclose the gaps (gate each → forward realtime captured at the source, not reconstructed):")
        for r in g:
            print("  spendguard install-hook --venv %s" % r["venv"].replace(os.path.expanduser("~"), "~"))
    else:
        print("\n✓ every LLM-calling venv is gated (or self-gates) — no ungated realtime sources.")
    return 1 if g else 0
