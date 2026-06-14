"""Per-model learnings — verified facts + quirks, AUTO-APPLIED whenever a model is used/tested/reviewed.

A fact that exists but isn't applied is useless (e.g. "gpt-5 wants reasoning='none'" sitting in memory
while a call burns its whole budget on reasoning and returns empty). So this module is the single place
that (a) knows each model family's quirks, (b) lets verified per-model learnings be stored and override
the defaults, and (c) APPLIES them to a call's kwargs automatically. experiment/cache-test/compare call
apply_call_params() so they can't forget.

Family rules below are the seed (verified facts); `add_fact()` stores model-specific learnings (e.g. an
experiment that proved reasoning='minimal' is cheapest at equal accuracy, or that a tier under-performs on
an intent) which then surface in `profile()` and CLI. Per-model best-practices are highly shareable.
"""
import re
from . import config

# ordered family rules — first match wins; later stored facts override fields
_RULES = [
    (r"^(gpt-5|o[1345])", dict(provider="openai", reasoning="minimal", tokens_param="max_completion_tokens",
        cache="auto", cache_min=1024,
        note="REASONING model: set reasoning_effort≈'none' (minimal) or it spends the whole budget on "
             "reasoning and returns EMPTY; rejects max_tokens (use max_completion_tokens). OpenAI auto-caches "
             "≥1024-tok identical static-first prefix (read 0.5x).")),
    (r"^gpt-", dict(provider="openai", reasoning=None, tokens_param="max_tokens", cache="auto", cache_min=1024,
        note="OpenAI auto-caches ≥1024-tok identical static-first prefix (read 0.5x).")),
    (r"^claude-(haiku|3-5-haiku|3-haiku)", dict(provider="anthropic", reasoning=None, tokens_param="max_tokens",
        cache="explicit", cache_min=2048,
        note="Anthropic Haiku cache minimum = 2048 tokens; explicit cache_control (read 0.1x / write 1.25x), static-first.")),
    (r"^claude-", dict(provider="anthropic", reasoning=None, tokens_param="max_tokens", cache="explicit", cache_min=1024,
        note="Anthropic Opus/Sonnet cache minimum = 1024 tokens; explicit cache_control (read 0.1x / write 1.25x), static-first.")),
]


def _db():
    # reuse the learn db connection (shared sqlite); a tiny model_facts table
    from . import learn
    with learn._lock:
        learn._db().execute("""CREATE TABLE IF NOT EXISTS model_facts(
            model TEXT, key TEXT, value TEXT, confidence REAL, source TEXT, verified INTEGER, ts TEXT,
            PRIMARY KEY (model, key))""")
        learn._db().commit()
    return learn


def _family(model):
    for pat, d in _RULES:
        if re.match(pat, str(model or "")):
            return dict(d)
    return dict(provider="?", reasoning=None, tokens_param="max_tokens", cache="?", cache_min=1024, note="")


def add_fact(model, key, value, confidence=0.9, source="manual", verified=True):
    """Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',
    'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5')."""
    L = _db()
    with L._lock:
        L._db().execute("INSERT OR REPLACE INTO model_facts VALUES (?,?,?,?,?,?,?)",
                        (model, key, str(value), float(confidence), source, 1 if verified else 0, learn_now()))
        L._db().commit()


def facts(model):
    L = _db()
    with L._lock:
        return {k: (v, c, src, bool(ver)) for k, v, c, src, ver in
                L._db().execute("SELECT key,value,confidence,source,verified FROM model_facts WHERE model=?",
                                (model,)).fetchall()}


def profile(model):
    """Family defaults merged with stored per-model facts (facts win)."""
    p = _family(model)
    p["model"] = model
    f = facts(model)
    for k, (v, _c, _src, _ver) in f.items():
        p[k] = v
    p["_facts"] = f
    return p


def apply_call_params(model, kw):
    """Mutate a chat-call kwargs dict to use the model correctly — the AUTO-APPLY that prevents the
    'forgot reasoning=none → empty output' class of bug. Returns kw."""
    p = profile(model)
    if p.get("tokens_param") == "max_completion_tokens" and "max_tokens" in kw:
        kw["max_completion_tokens"] = kw.pop("max_tokens")
    if p.get("provider") == "openai" and p.get("reasoning"):
        kw.setdefault("reasoning_effort", p["reasoning"])
    return kw


def learn_now():
    from . import learn
    return learn._now()


def cmd(argv=None):
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    if argv and argv[0] == "show":
        model = argv[1] if len(argv) > 1 else ""
        p = profile(model)
        print(f"model profile — {model}")
        for k in ("provider", "reasoning", "tokens_param", "cache", "cache_min"):
            print(f"  {k:<14} {p.get(k)}")
        print(f"  note           {p.get('note')}")
        if p.get("_facts"):
            print("  stored learnings:")
            for k, (v, c, src, ver) in p["_facts"].items():
                print(f"    {k:<22} {v}   ({src}, conf {c:.2f}{', verified' if ver else ''})")
        return 0
    # list known families + any models with stored facts
    print("model families (seed rules):")
    for pat, d in _RULES:
        print(f"  {pat:<28} reasoning={d['reasoning']}  tokens={d['tokens_param']}  cache={d['cache']}(min {d['cache_min']})")
    L = _db()
    with L._lock:
        rows = L._db().execute("SELECT DISTINCT model FROM model_facts").fetchall()
    if rows:
        print("models with stored learnings: " + ", ".join(r[0] for r in rows))
    print("  `spendguard models show <model>` for the full profile.")
    return 0
