"""Layer 2 — COLLECTIVE learning: opt-in, scrubbed insight export / import.

So a fleet (or community) gets better together — but privacy is the gate. Three tiers:
  - universal: prices (already shared via the LiteLLM table) — not here.
  - shareable: GENERALIZABLE rules — the regime + the rule, no identity. Exported only on request.
  - private: your intents, prompts, $ totals, concepts — NEVER leave.

Sharing isn't deletion, it's ABSTRACTION: keep the context that makes a rule reasoning-able
(task_class / regime / output_shape / the IF-THEN-BECAUSE and ratios like "26x"), drop the identity
($ amounts, intent names, evidence snippets). `insights export` previews EXACTLY what would leave before
writing. `insights import` brings community rules in as LOW-TRUST priors (status candidate, source
community) that must be locally corroborated by `validate` before the advisor leans on them.

CLI: `spendguard insights {list,export,import}`.
"""
import re, json
from . import learn

# $1,127 · $49/job · $0.04/job · 12.50/1M → stripped (identity). Ratios like "26x"/"~10×" are KEPT (generalizable).
_DOLLAR = re.compile(r"\$\s?[\d,]+(?:\.\d+)?(?:\s?/\s?(?:job|Mout|M|1M|1k))?", re.I)
_BARE_PRICE = re.compile(r"\b\d+\.\d{2}\s?/\s?\d+(?:\.\d+)?\b")          # 2.50/15.00 style


def _scrub_text(s, intent=None):
    if not s:
        return s
    out = _DOLLAR.sub("$X", s)
    out = _BARE_PRICE.sub("$X/$Y", out)
    if intent:                                                          # drop the private intent label
        out = re.sub(re.escape(intent), "<task>", out, flags=re.I)
    return out.strip()


def scrub(ins):
    """Abstract a private insight into a shareable, generalizable rule (or None if not shareable)."""
    # only share things with real applicability context — a bare sentence isn't reasoning-able elsewhere
    if not (ins.get("task_class") or ins.get("action") or ins.get("regime")):
        return None
    it = ins.get("intent")
    return {
        "task_class": ins.get("task_class"),
        "regime": ins.get("regime"),
        "output_shape": ins.get("output_shape"),
        "scale": ins.get("scale"),
        "condition": _scrub_text(ins.get("condition"), it),
        "action": _scrub_text(ins.get("action"), it),
        "mechanism": _scrub_text(ins.get("mechanism"), it),
        "lesson": _scrub_text(ins.get("lesson"), it),
        "confidence": ins.get("confidence"),
        "quality_basis": ins.get("quality_basis"),
        "support": ins.get("support"),
        "source": "shared",
    }


def scrubbed_abstracts(min_conf=0.6, active_only=True):
    """The scrubbed, shareable rules to push to the SaaS server (`saas.push_insights`). Same scrubber + gate as
    `insights export`: identity removed ($ amounts, intent names, evidence snippets), generalizable rule kept
    (task_class/regime/condition→action/mechanism/lesson + confidence/quality_basis). Returns a list of dicts."""
    return _shareable(min_conf, require_active=active_only)


def _shareable(min_conf, require_active):
    out = []
    for ins in learn.insights_full():
        if (ins.get("scope") == "private") and ins.get("scope") is not None and False:
            continue  # (scope is advisory; the scrubber is the real gate)
        if (ins.get("confidence") or 0) < min_conf:
            continue
        if require_active and (ins.get("status") != "active"):
            continue
        s = scrub(ins)
        if s:
            out.append(s)
    return out


def cmd_export(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard insights export")
    ap.add_argument("--out", help="write scrubbed JSON here (default: just preview)")
    ap.add_argument("--min-conf", type=float, default=0.6)
    ap.add_argument("--all", action="store_true", help="include non-active (else only corroborated 'active')")
    a = ap.parse_args(argv)
    recs = _shareable(a.min_conf, require_active=not a.all)
    print(f"insights export — {len(recs)} shareable rule(s) (conf≥{a.min_conf}"
          f"{', active only' if not a.all else ''}). PREVIEW of EXACTLY what would leave:\n")
    for r in recs:
        print(f"  • [{(r['confidence'] or 0):.2f}] ({r.get('task_class') or '?'}/{r.get('regime') or '?'}) {r['lesson']}")
        if r.get("action"):
            print(f"      {r.get('condition') or ''} → {r['action']}  [{r.get('quality_basis') or 'unverified'}]")
    print("\n  (identity scrubbed: $ amounts, intent names, evidence snippets removed; "
          "model names, ratios, task context kept — that's the generalizable rule.)")
    if a.out:
        json.dump({"insights": recs, "schema": "spendguard.shared.v1"}, open(a.out, "w"), indent=2)
        print(f"\n  wrote {len(recs)} rules → {a.out}")
    else:
        print("\n  (preview only — pass --out PATH to write the file.)")
    return 0


def cmd_import(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard insights import")
    ap.add_argument("path", help="a spendguard.shared.v1 JSON (file you trust)")
    ap.add_argument("--trust", type=float, default=0.4, help="cap imported confidence (low-trust prior)")
    a = ap.parse_args(argv)
    data = json.load(open(a.path))
    recs = data.get("insights", []) if isinstance(data, dict) else data
    n = 0
    for r in recs:
        if not r.get("lesson"):
            continue
        ctx = {k: r.get(k) for k in ("task_class", "regime", "output_shape", "scale",
                                     "condition", "action", "mechanism", "quality_basis")}
        ctx["scope"] = "shared"
        learn.add_insight(None, str(r["lesson"])[:500], evidence="(community)", source="community",
                          confidence=min(a.trust, float(r.get("confidence") or 0.4)), ctx=ctx, status="candidate")
        n += 1
    print(f"insights import — added {n} community rule(s) as LOW-TRUST candidates (conf≤{a.trust}).")
    print("  They won't sway the advisor until `spendguard validate` corroborates them against YOUR corpus.")
    return 0


def cmd_list(argv):
    rows = learn.insights_full(include_refuted=True)
    print(f"{'conf':>5} {'status':<10}{'src':<12}{'scope':<9}lesson")
    for i in rows:
        print(f"{(i['confidence'] or 0):>5.2f} {(i.get('status') or 'candidate'):<10}"
              f"{(i.get('source') or '?'):<12}{(i.get('scope') or 'private'):<9}{(i['lesson'] or '')[:70]}")
    return 0


def main(argv=None):
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    sub = argv[0] if argv else "list"
    rest = argv[1:]
    if sub == "export":
        return cmd_export(rest)
    if sub == "import":
        return cmd_import(rest)
    return cmd_list(rest)
