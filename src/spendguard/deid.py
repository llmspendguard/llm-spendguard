"""Client-side DE-IDENTIFICATION of the small amount of text that leaves this machine.

spendguard's privacy contract is that raw prompts/outputs and $-amounts NEVER leave; only generalized,
scrubbed rule-text, a caged "what was accomplished" digest, and git commit subjects sync to a team server
— and only when the user opts past `visibility=private`. This module is the DETERMINISTIC enforcement of
that contract at the wire: not a best-effort prompt instruction, a mechanical redactor that runs on every
egress payload.

It is a SAFETY / extraction step, NOT a meaning decision — so regex + an NER library are exactly the right
tools (the agentic boundary in CLAUDE.md is about deciding project / intent / quality, which still goes to
an LLM; redaction is the opposite kind of task). De-id is a tool TOWARD HIPAA Safe Harbor, not compliance
by itself — a covered entity still needs a BAA and its own review.

Configurable, client-side and local, via the `deid.*` settings (see config_schema):
  • engine=regex (DEFAULT): the deterministic FLOOR — zero deps, always available. A typed denylist of
    high-confidence identifiers (email, US phone, US SSN, credit-card w/ Luhn check, IPv4/IPv6, common
    API-key & bearer-token shapes, JWTs, PEM private-key blocks) plus the legacy $-amount scrub. Ratios
    like "26x" / "10×" are KEPT — they're the generalizable signal, not identity. Each hit becomes a typed
    tag: <EMAIL>, <PHONE>, <SSN>, <CREDIT_CARD>, <IP>, <API_KEY>, <PRIVATE_KEY>.
  • engine=presidio: the floor PLUS Microsoft Presidio NER (names, locations, dates, MRNs, the long tail).
    Opt-in — needs `pip install llm-spendguard[deid]`. If it isn't installed it DEGRADES to the floor and
    warns ONCE; it never raises into, or blocks, the egress path. (OpenMed/clinical NER is extraction, not
    de-id — wire such a model as a Presidio recognizer, not as the masker.)
  • engine=off: no redaction. A deliberate footgun for fully-trusted private data only.

Everything fails OPEN toward PRIVACY: on any internal error the floor still runs, and Presidio failures
fall back to the floor. The only way text leaves un-redacted is engine=off, which the user chooses
explicitly. `redact()` never raises.
"""
import os
import re
import sys

# ── the deterministic floor ──────────────────────────────────────────────────────────────────────────
# (NAME, compiled pattern, replacement, optional validator(match_text)->bool). Order matters: the most
# specific / most structured shapes run first so a token isn't half-consumed by a looser rule.

_DOLLAR = re.compile(r"\$\s?[\d,]+(?:\.\d+)?(?:\s?/\s?(?:job|Mout|M|1M|1k))?", re.I)  # $1,127 · $49/job · 12.50/1M
_BARE_PRICE = re.compile(r"\b\d+\.\d{2}\s?/\s?\d+(?:\.\d+)?\b")                       # 2.50/15.00 style


def _luhn(s):
    """A 13–19 digit run is only a credit card if it passes Luhn — keeps random long IDs from being masked."""
    digits = [int(c) for c in re.sub(r"\D", "", s)]
    if not (13 <= len(digits) <= 19):
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_RULES = [
    # secrets / keys (before EMAIL: some token shapes contain '@'-free but key-like runs)
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<PRIVATE_KEY>", None),
    ("API_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"), "<API_KEY>", None),       # Anthropic (before sk-)
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"), "<API_KEY>", None),           # OpenAI
    ("API_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<API_KEY>", None),               # AWS access key id
    ("API_KEY", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "<API_KEY>", None),         # Google API key
    ("API_KEY", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "<API_KEY>", None),     # GitHub token
    ("API_KEY", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<API_KEY>", None),    # Slack token
    ("API_KEY", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<API_KEY>", None),  # JWT
    ("API_KEY", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{16,}"), "<API_KEY>", None), # generic bearer
    # direct identifiers
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<EMAIL>", None),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<SSN>", None),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[ .\-]?)?(?:\(\d{3}\)|\d{3})[ .\-]\d{3}[ .\-]\d{4}(?!\d)"), "<PHONE>", None),
    ("CREDIT_CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), "<CREDIT_CARD>", _luhn),
    ("IP", re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"), "<IP>", None),  # IPv6 (≥3 hextets, not a time)
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), "<IP>", None),  # IPv4
    # money (identity); ratios like "26x" have no $ and are intentionally KEPT
    ("USD", _DOLLAR, "$X", None),
    ("PRICE", _BARE_PRICE, "$X/$Y", None),
]

#: the entity names the floor can redact (for docs / config validation / the `entities` filter).
FLOOR_ENTITIES = tuple(dict.fromkeys(name for name, *_ in _RULES))


def _engine(explicit=None):
    if explicit:
        return explicit.strip().lower()
    env = os.getenv("SPENDGUARD_DEID_ENGINE")
    if env:
        return env.strip().lower()
    try:
        from . import config
        return str(config._cfg_get("deid", "engine", "regex")).strip().lower()
    except Exception:
        return "regex"


def _entity_set(entities):
    if not entities:
        return None
    items = entities.split(",") if isinstance(entities, str) else entities
    return {e.strip().upper() for e in items if e and e.strip()}


def _floor(text, entities=None):
    allow = _entity_set(entities)
    out = text
    for name, pat, repl, validator in _RULES:
        if allow is not None and name not in allow:
            continue
        if validator is None:
            out = pat.sub(repl, out)
        else:
            out = pat.sub(lambda m, r=repl, v=validator: r if v(m.group(0)) else m.group(0), out)
    return out


_WARNED = set()


def _warn_once(msg):
    if msg not in _WARNED:
        _WARNED.add(msg)
        print("[spendguard.deid] " + msg, file=sys.stderr)


_PRESIDIO = None  # None=untried, False=unavailable, (analyzer, anonymizer)=ready


def _presidio(text, entities=None):
    """The floor has ALREADY been applied to `text`; this layers NER on top. Returns the anonymized string,
    or None on any problem so the caller keeps the floored text. Never raises."""
    global _PRESIDIO
    if _PRESIDIO is False:
        return None
    if _PRESIDIO is None:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            _PRESIDIO = (AnalyzerEngine(), AnonymizerEngine())
        except Exception:
            _PRESIDIO = False
            _warn_once("deid.engine=presidio but Presidio isn't installed — using the regex floor only. "
                       "Enable it with `pip install llm-spendguard[deid]`.")
            return None
    try:
        analyzer, anonymizer = _PRESIDIO
        ents = None
        if entities:
            ents = [e.strip() for e in (entities.split(",") if isinstance(entities, str) else entities) if e and e.strip()]
        results = analyzer.analyze(text=text, language="en", entities=ents)
        return anonymizer.anonymize(text=text, analyzer_results=results).text
    except Exception:
        return None


def _drop_literals(text, drop):
    """Remove caller-named private labels (e.g. the intent string) → <task>. Honored regardless of engine,
    because it's an explicit instruction from the caller, not part of the configurable PII floor."""
    if not drop:
        return text
    for d in (drop if not isinstance(drop, str) else [drop]):
        if d:
            text = re.sub(re.escape(d), "<task>", text, flags=re.I)
    return text


def redact(text, *, engine=None, entities=None, drop=None):
    """De-identify `text` before it leaves this machine. The single chokepoint for every egress path.

    engine: override the configured `deid.engine` (regex | presidio | off). None = use config / env.
    entities: restrict to these entity types (list or comma-string); None = all.
    drop: literal private labels to strip to <task> (always honored, even when engine=off).

    Non-str / empty input passes through. Fails open toward privacy: never raises; on any error the
    deterministic floor still applies (only engine=off skips it)."""
    if not isinstance(text, str) or not text.strip():
        return text
    eng = _engine(engine)
    out = text
    if eng != "off":
        try:
            out = _floor(out, entities)
        except Exception:
            pass
        if eng == "presidio":
            p = _presidio(out, entities)
            if p is not None:
                out = p
    return _drop_literals(out, drop)
