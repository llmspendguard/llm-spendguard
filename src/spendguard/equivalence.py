"""Graded equivalence ladder (promptfoo-style) — the experiment lab's "same output?" gate.

Borrowed from promptfoo's assertion model: equivalence is a LADDER of checks, cheapest/deterministic
first, paid/semantic last — not one judge call. Run the cheapest tier that can decide; escalate to the
caged semantic tiers only when asked. grade() returns (score 0..1, tier):

  exact      normalized string identity                                            (free)
  structural same JSON shape/keys/length — the format/contract is preserved         (free)
  scalar     fraction of scalar fields equal — value agreement   [default for JSON] (free)
  text       difflib ratio                                       [fallback prose]   (free)
  embed      embedding cosine (semantic) — for prose where text-ratio is too literal (CAGED: 1 embed call/side)
  rubric     LLM judge "equivalent for the task? 0..1"                              (CAGED: 1 judge call) — last resort

structural is reported alongside the value score because for cost work they answer different questions:
"does the cheaper model still emit the right SHAPE (pipeline won't break)?" vs "are the VALUES the same?"
"""
import re, json, difflib

_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", (s or "").strip())


def _norm_json(s):
    if not s:
        return None
    for a, b in (("{", "}"), ("[", "]")):
        i, j = s.find(a), s.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                pass
    return None


def _flatten(x):
    if isinstance(x, list):
        for e in x:
            yield from _flatten(e)
    elif isinstance(x, dict):
        for k in sorted(x):
            yield from _flatten(x[k])
    else:
        yield x


def _shape(x):
    """Structural fingerprint: types + dict keys + list lengths, ignoring scalar VALUES."""
    if isinstance(x, dict):
        return ("dict", tuple(sorted((k, _shape(v)) for k, v in x.items())))
    if isinstance(x, list):
        return ("list", len(x), tuple(_shape(e) for e in x[:50]))
    return type(x).__name__


def structural(ref, out):
    """True if both parse to JSON with the same shape (keys/lengths/types) — format/contract preserved."""
    a, b = _norm_json(ref), _norm_json(out)
    if a is None or b is None:
        return None
    return _shape(a) == _shape(b)


def _scalar(a, b):
    fa, fb = list(_flatten(a)), list(_flatten(b))
    n = max(len(fa), len(fb))
    if not n:
        return 1.0
    return sum(1 for i in range(n) if i < len(fa) and i < len(fb) and fa[i] == fb[i]) / n


def _text_ratio(ref, out):
    return difflib.SequenceMatcher(None, _norm(ref), _norm(out)).ratio()


def _embed_cosine(ref, out, model="text-embedding-3-small"):
    """Semantic similarity via embeddings (CAGED — caller wraps in spendguard:* context). 0..1."""
    from openai import OpenAI
    from . import config
    c = OpenAI(api_key=config.api_key("OPENAI_API_KEY"))
    r = c.embeddings.create(model=model, input=[ref[:8000], out[:8000]])
    va, vb = r.data[0].embedding, r.data[1].embedding
    dot = sum(x * y for x, y in zip(va, vb))
    na = sum(x * x for x in va) ** 0.5
    nb = sum(y * y for y in vb) ** 0.5
    return max(0.0, dot / (na * nb)) if na and nb else 0.0


_RUBRIC = ("Are these two answers EQUIVALENT for the task (same meaning/result, ignore formatting)? "
           "Reply with ONLY a number 0.0 to 1.0.\n\nA:\n{a}\n\nB:\n{b}")


def _llm_rubric(ref, out, model):
    """LLM judge of semantic equivalence (CAGED). Returns 0..1."""
    from . import adapters
    r = adapters.call(model, _RUBRIC.format(a=ref[:3000], b=out[:3000]), max_tokens=8)
    m = re.search(r"[01](?:\.\d+)?", r.get("text") or "")
    return float(m.group()) if m else 0.0


def grade(ref, out, mode="auto", model=None):
    """(score 0..1, tier).

    mode='auto' (free): exact → scalar (JSON value-fraction) → text. NOTE the scalar tier compares fields
    POSITIONALLY (after sorting dict keys) — correct for ordered per-item outputs (the i-th label), but it
    scores reordered/set-style or free-text-valued JSON as different. When order/format varies or values
    are free text, pass mode='embed'/'rubric' — those CAGED semantic tiers now apply even to JSON (they
    used to be silently skipped for any JSON pair)."""
    if _norm(ref) == _norm(out):
        return 1.0, "exact"
    if mode == "embed":                       # explicit semantic tier — applies even to JSON
        return _embed_cosine(ref, out), "embed"
    if mode == "rubric":
        return _llm_rubric(ref, out, model or "claude-haiku-4-5"), "rubric"
    a, b = _norm_json(ref), _norm_json(out)
    if a is not None and b is not None:
        return _scalar(a, b), "scalar"
    return _text_ratio(ref, out), "text"
