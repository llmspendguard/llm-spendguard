"""cache-audit — find where PROMPT CACHING would cut spend, and prove it's working.

Anthropic/OpenAI bill full price for input UNLESS a repeated prefix is cached (Anthropic: explicit
cache_control, read ≈0.1× base, write 1.25×; OpenAI: auto-caches prefixes ≥1024 tokens, read 0.5×).
The win is a LARGE prefix REUSED across many calls — a big system prompt / few-shot block — not
data-heavy prompts whose bulk varies per call.

spendguard has the two things needed to find it:
  - call_io  → the real prompts: longest common prefix per intent + what FRACTION of the prompt it is.
  - scripts  → the realtime/agent system prompts (big string constants) that get reused across calls.
  - usage    → realized cache hit rate (cached vs total input), forward via the gate — proves adoption.

This reports the prefix fraction (high fraction = cache it), the savings per 1000 calls, the big
script-defined system prompts to cache, and per-provider setup. CLI: `spendguard cache-audit [--repo]`.
"""
import os, re, glob
from . import callio, calls, pricing
from .submit import _count_tokens

# OpenAI auto-caches only prefixes at/above this; Anthropic explicit cache_control has lower minimums.
_MIN_CACHE_TOKENS = 1024


def _common_prefix(strs):
    if not strs:
        return ""
    s1, s2 = min(strs), max(strs)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    cp = s1[:i]
    return cp[:cp.rfind("\n") + 1] if "\n" in cp else cp           # clean boundary


def _intent_prefixes():
    out = []
    with callio._lock:
        combos = callio._db().execute(
            "SELECT COALESCE(intent,'(none)'), model, COUNT(*) FROM call_io WHERE prompt!='' "
            "GROUP BY intent, model HAVING COUNT(*) >= 3").fetchall()
    for intent, model, n in combos:
        with callio._lock:
            prompts = [r[0] for r in callio._db().execute(
                "SELECT prompt FROM call_io WHERE COALESCE(intent,'(none)')=? AND model=? AND prompt!='' LIMIT 30",
                (intent, model)).fetchall()]
        if len(prompts) < 3:
            continue
        pref = _count_tokens(_common_prefix(prompts), model)
        avg = max(1, sum(_count_tokens(p, model) for p in prompts) // len(prompts))
        try:
            p = pricing.price(model)
            delta = p["in_"] - p.get("cached_in", p["in_"])
        except Exception:
            delta = 0
        save_per_1k = pref * 1000 * delta / 1_000_000           # $ saved / 1000 cached calls
        out.append(dict(intent=intent, model=model, n=n, prefix=pref, avg=avg,
                        frac=pref / avg, save_per_1k=save_per_1k))
    return sorted(out, key=lambda d: -d["save_per_1k"])


_SYS_ASSIGN = re.compile(r"""(?:system\s*=|_SYS\b\s*=|SYSTEM\s*=|system_prompt\s*=|SYSTEM_PROMPT\s*=)\s*\(?\s*("{1,3}|'{1,3})""")


def _script_system_prompts(repo, min_tokens=400):
    """Big string constants that look like system/instruction prompts — realtime cache candidates."""
    hits = []
    for path in glob.glob(os.path.join(repo, "**", "*.py"), recursive=True):
        if "/.git/" in path or "/site-packages/" in path:
            continue
        try:
            txt = open(path, errors="ignore").read()
        except Exception:
            continue
        for m in _SYS_ASSIGN.finditer(txt):
            q = m.group(1)
            start = m.end()
            end = txt.find(q, start)
            if end < 0:
                continue
            body = txt[start:end].strip()
            # prose-like guard: real prompts start with a word & read like instructions, not code/defaults
            if not body[:1].isalpha() or " " not in body[:30]:
                continue
            if not re.search(r"\b(you are|given|classify|return|for each|output|task|expert|assistant|"
                             r"respond|estimate|judge|extract|rules?)\b", body[:400], re.I):
                continue
            tk = _count_tokens(body, "gpt-5.5")
            if tk >= min_tokens:
                hits.append((os.path.relpath(path, repo), tk, body[:80].replace("\n", " ")))
    return sorted(set(hits), key=lambda h: -h[1])[:15]


def _realized_hit_rate():
    """cached vs total input from whatever usage we have (the gate realtime log). 0 known = opportunity unrealized."""
    from .config import RT_LOG
    import json
    tot = cached = 0
    if os.path.exists(RT_LOG):
        for ln in open(RT_LOG, errors="ignore"):
            try:
                e = json.loads(ln)
            except Exception:
                continue
            tot += e.get("in_tok", 0) or 0
            cached += e.get("cached_in_tok", 0) or 0
    return (cached / tot) if tot else None, tot


def audit(repo=None):
    repo = repo or os.getcwd()
    print("cache-audit — where prompt caching would cut spend (read ≈0.1× base Anthropic / 0.5× OpenAI)\n")

    rate, seen = _realized_hit_rate()
    if rate is not None:
        print(f"realized cache hit rate (gate realtime log): {100*rate:.0f}%  over {seen:,} input tokens"
              + ("   ← low: prefixes aren't being cached" if rate < 0.3 else ""))
    else:
        print("realized cache hit rate: no instrumented realtime calls yet "
              "(the gate now records cached_in_tok going forward — re-check after some calls).")

    print("\nrepeated-prefix opportunity in recovered prompts (call_io):")
    print(f"  {'intent':<22}{'model':<20}{'prefix':>7}{'avg':>7}{'frac':>6}{'$/1k calls':>11}")
    rows = _intent_prefixes()
    for d in rows[:12]:
        flag = "  ← cache" if (d["frac"] >= 0.4 and d["prefix"] >= 200) else ""
        print(f"  {d['intent'][:21]:<22}{d['model'][:19]:<20}{d['prefix']:>7}{d['avg']:>7}"
              f"{d['frac']:>6.0%}{('$%.2f' % d['save_per_1k']):>11}{flag}")
    big = [d for d in rows if d["frac"] >= 0.4 and d["prefix"] >= 200]
    if not big:
        print("  → these are DATA-heavy prompts (small shared prefix) — caching won't help much here.")

    print("\nbig system prompts in scripts (realtime/agent calls — where the real savings usually are):")
    sp = _script_system_prompts(repo)
    if sp:
        for path, tk, head in sp[:10]:
            print(f"  ~{tk:>5} tok  {path}   “{head}…”")
        print("  → if any of these run across many calls, caching the system block saves ~90% of ITS input (Anthropic).")
    else:
        print("  (none ≥400 tokens found in repo scripts.)")

    print("\nhow to capture it, per provider:")
    print("  • Anthropic: add a cache_control:{type:'ephemeral'} breakpoint after the static system/few-shot block;")
    print("    read = 0.1× base, write = 1.25× (pays back after ~2 reuses), 5-min TTL. Put STATIC content first.")
    print("  • OpenAI: caching is automatic for identical prefixes ≥1024 tokens — order static (system, schema,")
    print("    few-shot) BEFORE the variable user content so the prefix matches; read = 0.5× base.")
    print("  • Verify: re-run cache-audit — the realized hit rate (above) should climb. That closes the 28% gap.")
    return dict(realized=rate, opportunities=len(big), scripts=len(sp))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard cache-audit")
    ap.add_argument("--repo", help="repo to scan for big system prompts (default: cwd)")
    a = ap.parse_args(argv)
    audit(a.repo)
    return 0
