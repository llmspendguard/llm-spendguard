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
import json
import re

# ordered family rules — first match wins; later stored facts override fields
_RULES = [
    # VERIFIED 2026-06-14: the literal "no reasoning" value DIFFERS by model and the wrong one is a hard 400.
    (r"^gpt-5\.5", dict(provider="openai", reasoning="none", tokens_param="max_completion_tokens",
        cache="auto", cache_min=1024,
        note="gpt-5.5: reasoning_effort='none' (verified) — 'minimal' is REJECTED (400). Rejects max_tokens "
             "(use max_completion_tokens). OpenAI auto-caches ≥1024-tok static-first prefix (read 0.5x).")),
    (r"^(gpt-5|o[1345])", dict(provider="openai", reasoning="minimal", tokens_param="max_completion_tokens",
        cache="auto", cache_min=1024,
        note="gpt-5 mini/nano + o-series: reasoning_effort='minimal' (verified) — 'none' is REJECTED (400); "
             "without it reasoning eats the budget → EMPTY output. Rejects max_tokens (use max_completion_tokens). "
             "OpenAI auto-caches ≥1024-tok static-first prefix (read 0.5x).")),
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
        # TYPE SURVIVES THE ROUND TRIP. str(value) turned every fact into text, and profile() lets facts
        # override family defaults — so storing a numeric fact like cache_min_tokens=4096 replaced an int
        # default with "4096", and the next `prefix >= p["cache_min_tokens"]` compared int to str: a
        # TypeError on Python 3, or a silently wrong answer anywhere the value is used in a truthy test
        # (bool("False") is True, which is the same bug for a boolean fact). JSON keeps the type; the
        # reader below falls back to the raw text so rows already written as bare strings still load.
        L._db().execute("INSERT OR REPLACE INTO model_facts VALUES (?,?,?,?,?,?,?)",
                        (model, key, json.dumps(value), float(confidence), source,
                         1 if verified else 0, learn_now()))
        L._db().commit()


def clear_fact(model, key):
    """Delete a per-model learning — for a fact recorded WRONG (e.g. an auto-heal that learned an implausible
    max_output from a NON-budget 400) that must stop overriding the family/table value. Returns rows removed; after
    this the model falls back to the family default and can re-learn correctly."""
    L = _db()
    with L._lock:
        cur = L._db().execute("DELETE FROM model_facts WHERE model=? AND key=?", (model, key))
        L._db().commit()
        return cur.rowcount


def facts(model):
    L = _db()
    with L._lock:
        return {k: (_decode_fact(v), c, src, bool(ver)) for k, v, c, src, ver in
                L._db().execute("SELECT key,value,confidence,source,verified FROM model_facts WHERE model=?",
                                (model,)).fetchall()}


def _decode_fact(raw):
    """Stored fact -> its original Python value. Rows written before add_fact stored JSON are bare strings
    (e.g. `minimal`, not `"minimal"`), which json.loads rejects — those load as the string they always were,
    so an existing store keeps working rather than raising on first read."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def profile(model):
    """Family defaults merged with stored per-model facts (facts win)."""
    p = _family(model)
    p["model"] = model
    f = facts(model)
    for k, (v, _c, _src, _ver) in f.items():
        p[k] = v
    p["_facts"] = f
    return p


def normalize_reasoning(model, level):
    """Map a STANDARD ordinal reasoning level → the reasoning_effort value THIS model actually accepts, so ONE knob
    (minimal|low|medium|high) works across OpenAI's inconsistent naming. Only the FLOOR varies per model — gpt-5.5
    wants 'none', gpt-5 mini/nano + o-series want 'minimal' (both VERIFIED against 400s in the family rules above);
    low/medium/high are the OpenAI API's own universal values. Returns: the model's verified FLOOR for level
    'minimal'; 'low'/'medium'/'high' unchanged; None for a NON-reasoning model (drop the param); the caller's value
    unchanged for an unrecognised level (their explicit choice — the send-side ladder still guards a 400).

    This is the OPENAI-FAMILY half, where the naming inconsistency the user hit actually lives. Anthropic (extended
    thinking = a token BUDGET, and it conflicts with a forced-tool schema) and Gemini (reasoning = a model-id SUFFIX,
    on the lane path) use different mechanisms and are normalised once MEASURED — never guessed (the models.py rule)."""
    lv = (level or "").strip().lower()
    if lv not in ("minimal", "low", "medium", "high"):
        return level                              # unrecognised → pass through; the caller meant it
    floor = profile(model).get("reasoning")
    if floor is None:
        return None                               # a verified NON-reasoning model (gpt-4, claude-haiku) → no effort param
    if lv == "minimal" and floor and floor != "?":
        return floor                              # this model's VERIFIED lowest effort ('none' | 'minimal')
    return lv                                     # low|medium|high are universal; 'minimal' on an unknown floor passes through


def apply_call_params(model, kw, *, dialect=None):
    """Mutate a chat-call kwargs dict to use the model correctly — the AUTO-APPLY that prevents the
    'forgot reasoning=none → empty output' class of bug. Returns kw.

    THE ONLY PLACE that turns a per-model fact into a request parameter. Every caller — adapters, experiment,
    cascade — comes through here. A second implementation is not a shortcut, it is a fact store that some
    calls consult and others do not, which is indistinguishable from having no fact at all on the paths that
    skipped it: measured, adapters.call carried its own inlined copy of this lookup while THIS function sat
    unused on the one path where the empty-output bug actually happened.

    `dialect` is the request SHAPE the caller has already chosen ('openai' for anything speaking Chat
    Completions — openai, moonshot, z.ai — or 'anthropic' for the Messages API). It must be passed by anyone
    who knows it, because the fallback (inferring from the model name via the family rules) answers '?' for
    every model no rule matches. Silently, and in exactly the wrong direction: `kimi-k3` and `glm-5.2` both
    have MEASURED reasoning facts written by an A/B, both speak the OpenAI shape, and both infer provider='?'
    — so name-inference alone would drop the very facts the A/B was run to establish."""
    p = profile(model)
    shape = dialect or p.get("provider")
    if p.get("tokens_param") == "max_completion_tokens" and "max_tokens" in kw:
        kw["max_completion_tokens"] = kw.pop("max_tokens")
    eff = p.get("reasoning")
    # '?' is the family-rule miss marker, not a tier any endpoint accepts. Sending it is a 400.
    if shape == "openai" and eff and eff != "?":
        kw.setdefault("reasoning_effort", eff)          # an explicit caller argument still wins
    return kw


def mark_ineffective(model, intent, reason, confidence=0.85):
    """Record that a model just doesn't work for an intent (or globally if intent falsy) — so future
    experiments/recommendations skip it instead of re-paying to rediscover it."""
    add_fact(model, f"ineffective:{intent or '*'}", reason, confidence=confidence, source="experiment", verified=True)


def ineffective(model, intent):
    """(reason, confidence, ts) if model is known-ineffective for this intent or globally, else None."""
    f = facts(model)
    L = _db()
    for key in (f"ineffective:{intent}", "ineffective:*"):
        if key in f:
            v, c, _src, _ver = f[key]
            with L._lock:
                r = L._db().execute("SELECT ts FROM model_facts WHERE model=? AND key=?", (model, key)).fetchone()
            return (v, c, r[0] if r else None)
    return None


def _rejected_param(err):
    """The request parameter a provider's error names as invalid — read from the SDK's TYPED fields, or "".

    `param` is a documented field with a fixed shape in the OpenAI-compatible error envelope
    (`{"error": {"message", "type", "param", "code"}}`), so reading it is parsing a known schema. The
    human-readable `message` beside it is prose each provider words as it likes — "does not support",
    "is not acceptable", "invalid value for" — and matching on that phrasing meant every rewording
    silently disabled healing. Nothing here looks at the message.

    Returns "" for anything without those fields (a bare string, a network error), and "" means do not
    heal — an error we cannot attribute to a parameter is not one to fix by guessing."""
    def _from_body():
        body = getattr(err, "body", None)
        if isinstance(body, dict):
            e = body.get("error")
            if isinstance(e, dict):
                return e.get("param")
        return None

    def _from_response():
        resp = getattr(err, "response", None)
        if resp is None:
            return None
        e = (resp.json() or {}).get("error")
        return e.get("param") if isinstance(e, dict) else None

    for get in (lambda: getattr(err, "param", None), _from_body, _from_response):
        try:
            v = get()
        except Exception:
            continue
        if v:
            return str(v)
    return ""


def heal_reasoning(model, kw, err):
    """If a call failed while sending a reasoning_effort this model does not take, substitute the literal its
    family DOES take and return True so the caller retries. Else False.

    Three things were wrong here, and all three came from guessing rather than consulting the table this
    module already maintains:

    1. IT READ THE PROVIDER'S PROSE. `"does not support" not in e` decided whether an error was about this
       parameter. That is a judgement about the meaning of a free-text message that providers word however
       they like — "is not acceptable", "invalid value for", "unsupported parameter" — and each rewording
       silently turns healing off. Nothing is parsed now: the question "is there a better value to send?"
       is answered from the capability table, which is a fact we own.
    2. IT FLIPPED A COIN BETWEEN TWO LITERALS. `"none" if sent == "minimal" else "minimal"` is only correct
       when the sent value is one of those two. Send "low", "high", or None — all real values — and it
       "healed" to "minimal", which for the gpt-5.5 family is precisely the value that gets rejected. It
       could therefore retry with the same failure, or swap a working value for a broken one.
    3. IT RECORDED THE GUESS AS VERIFIED. add_fact(..., verified=True) was written BEFORE the retry, so a
       value that had never once succeeded entered the fact store as verified truth — and facts beat family
       defaults in profile(), so one bad heal poisons every later call for that model. The fact is now
       written unverified, and only confirm_reasoning() below — called after a retry actually succeeds —
       marks it verified.

    Retries are bounded structurally: after healing, kw holds the table's value, so the next call finds
    want == sent and returns False. No attempt counter to get out of step."""
    if "reasoning_effort" not in kw:
        return False
    if _rejected_param(err) != "reasoning_effort":
        # Healing on ANY failure is worse than the substring it replaced: a transient rate-limit would flip
        # the literal, the retry would succeed for unrelated reasons, and confirm_reasoning() would write the
        # wrong value into the fact store as VERIFIED — poisoning every later call for this model. The
        # provider says which parameter it rejected in a typed field; that is what gets read.
        return False
    sent = kw.get("reasoning_effort")
    want = profile(model).get("reasoning")
    if not want or want == sent:
        return False      # nothing better to try — this failure is not this function's to fix
    kw["reasoning_effort"] = want
    add_fact(model, "reasoning", want, source=f"auto-heal(retry after {sent!r} failed)", verified=False)
    return True


def confirm_reasoning(model, kw):
    """Mark the healed reasoning literal VERIFIED — call after a healed retry actually succeeded.

    Exists because heal_reasoning must not claim verification for a value that has not yet worked. Without
    this the store would only ever hold unverified heals, which is the other half of the same bug: a fact
    that is true and never promoted is as useless as one that is false and trusted."""
    v = kw.get("reasoning_effort")
    if v:
        add_fact(model, "reasoning", v, source="auto-heal(confirmed by successful retry)", verified=True)


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
