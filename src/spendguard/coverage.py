"""Coverage audit — which python venvs CAN make LLM calls (an openai/anthropic SDK is installed) but are NOT gated
(no spendguard sitecustomize/usercustomize hook). Those are the ungated realtime SOURCES: their spend never hits the
live gate, so it only surfaces (if at all) in after-the-fact reconstruction — the completeness gap. The DURABLE fix is
to gate them at the source (`spendguard install-hook --venv <v>`); this audit makes the gaps visible + prints the fix.

`spendguard coverage` runs it. Roots are configurable (env SPENDGUARD_COVERAGE_ROOTS, colon-separated globs > config
`coverage.roots` > sensible defaults) so it stays portable — no hardcoded user paths.
"""
import os
import glob
import subprocess

from .adapters import llm_capability_import_names
from .setup import _PTH_SENTINEL, _SENTINEL


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


def _purelib(venv):
    """The venv interpreter's real purelib, or None. Never guess from stale versioned directories."""
    target = os.path.join(venv, "bin", "python")
    try:
        r = subprocess.run([target, "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
                           capture_output=True, text=True, check=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def _is_gated(venv, purelib=None):
    """A venv is gated only when its interpreter's real purelib carries an owned startup hook."""
    sp = purelib or _purelib(venv)
    if not sp:
        return False
    for hook in ("sitecustomize.py", "usercustomize.py", "spendguard_gate_hook.pth"):
        hp = os.path.join(sp, hook)
        try:
            if os.path.exists(hp):
                with open(hp, errors="ignore") as _fh:        # closed deterministically (was a leaked handle)
                    hook_text = _fh.read()
                    if _SENTINEL in hook_text or _PTH_SENTINEL in hook_text:
                        return True
        except Exception:
            pass
    return False


def _is_own(sp, venv):
    """True if this venv IS spendguard's own DEV environment (it self-gates via require() in its scripts, so it is
    not a coverage gap). Robust to a non-standard checkout path: the reliable signal is an EDITABLE spendguard
    install (an `__editable__`/`.pth` finder, or a dist-info direct_url marked editable), not just the dir name.
    A REGULAR pip install of spendguard as a dependency is NOT 'own' and should still be gated."""
    if "llm-spendguard" in venv or "llm_spendguard" in venv:
        return True
    if glob.glob(os.path.join(sp, "__editable__*spendguard*")) or glob.glob(os.path.join(sp, "*spendguard*.pth")):
        return True
    import json
    for dinfo in glob.glob(os.path.join(sp, "llm_spendguard*.dist-info", "direct_url.json")):
        try:
            with open(dinfo) as _fh:
                if (json.load(_fh).get("dir_info") or {}).get("editable"):
                    return True
        except Exception:
            pass
    return False


def audit_gating_coverage(roots=None):
    """[{venv, llm:[providers], gated:bool, own:bool}] for every venv (under roots) that has an LLM SDK installed.
    `own` = it's spendguard's own venv, which self-gates via `import spendguard; spendguard.require()` in its scripts
    (gating it via sitecustomize would spam the receipt on every dev `python` — so that's intentional, not a gap)."""
    pats = _roots() if roots is None else roots   # explicit roots=[] means scan NOTHING, not fall back to defaults
    seen, out = set(), []
    for pat in pats:
        for venv in glob.glob(os.path.expanduser(pat)):
            sp = _purelib(venv)
            if not sp or sp in seen:
                continue
            seen.add(sp)
            llm = sorted(p for p in llm_capability_import_names() if os.path.isdir(os.path.join(sp, p)))
            if not llm:
                continue
            out.append({"venv": venv, "llm": llm, "gated": _is_gated(venv, sp),
                        "own": _is_own(sp, venv)})
    return out


def gaps(rows=None, roots=None):
    """The ungated LLM venvs that are NOT spendguard's own — the real coverage gaps to gate.

    `rows` lets a caller that has already run audit() reuse it instead of re-scanning every venv on the
    machine; the definition of "a gap" stays here either way."""
    return [r for r in (audit_gating_coverage(roots) if rows is None else rows)
            if r["llm"] and not r["gated"] and not r["own"]]


def _ledger_code_crosscheck(rows, captured_sources):
    """Return unmatched code venvs and ledger source paths using mechanical path containment."""
    def matches(source, venv):
        source_path = os.path.realpath(os.path.expanduser(source))
        venv_path = os.path.realpath(os.path.expanduser(venv))
        try:
            return os.path.commonpath((source_path, venv_path)) in (source_path, venv_path)
        except ValueError:
            return False

    code_venvs = {row["venv"] for row in rows if row["llm"]}
    ledger_sources = {source for source in captured_sources if source}
    unmatched_code = sorted(venv for venv in code_venvs
                            if not any(matches(source, venv) for source in ledger_sources))
    unmatched_ledger = sorted(source for source in ledger_sources
                              if not any(matches(source, venv) for venv in code_venvs))
    return unmatched_code, unmatched_ledger


def _captured_spend_sources():
    from .ledger import SpendLedger
    return SpendLedger().captured_spend_source_paths()


def cmd(argv=None):
    rows = audit_gating_coverage()
    print("spendguard coverage — LLM-calling venvs (gated = live realtime captured at the source, no reconstruction):")
    if not rows:
        print("  (no venvs with an LLM SDK found under the scanned roots; set SPENDGUARD_COVERAGE_ROOTS to widen)")
    # WHAT COUNTS AS A GAP IS DEFINED IN gaps(), ONCE. This loop used to rebuild the same condition inline
    # (`not gated and not own`) while gaps() sat directly above it with no caller — so the definition of "a
    # coverage gap" lived in two places, free to drift, and the named one was the copy nobody exercised.
    g = gaps(rows)
    gap_venvs = {r["venv"] for r in g}
    for r in sorted(rows, key=lambda x: x["venv"]):
        v = r["venv"].replace(os.path.expanduser("~"), "~")
        if r["venv"] in gap_venvs:
            tag = "🔴 UNGATED — its realtime spend slips live capture"
        elif r["gated"]:
            tag = "🟢 gated"
        elif r["own"]:
            tag = "🟡 self-gates (spendguard's own venv — scripts call spendguard.require())"
        else:
            tag = "⚪ no LLM SDK"
        print("  %-50s LLM=%-16s %s" % (v, ",".join(r["llm"]), tag))
    if g:
        print("\nclose the gaps (gate each → forward realtime captured at the source, not reconstructed):")
        for r in g:
            print("  spendguard install-hook --venv %s" % r["venv"].replace(os.path.expanduser("~"), "~"))
    else:
        print("\n✓ every LLM-calling venv is gated (or self-gates) — no ungated realtime sources.")
    try:
        captured_sources = _captured_spend_sources()
    except Exception as exc:
        print("\nledger×code cross-check unavailable — %s: %s" % (type(exc).__name__, exc))
    else:
        unmatched_code, unmatched_ledger = _ledger_code_crosscheck(rows, captured_sources)
        for venv in unmatched_code:
            print("\n⚠️ LLM-capable but no captured spend — not run yet OR running ungated; "
                  "verify with `spendguard doctor` in that venv. [%s]" % venv)
        for source in unmatched_ledger:
            print("\n🔴 captured spend but no LLM code path found — a missed transitive path or an "
                  "attribution bug; investigate. [%s]" % source)
    return 1 if g else 0
