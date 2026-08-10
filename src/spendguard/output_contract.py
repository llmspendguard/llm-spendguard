"""The SHAPE a job's output must arrive in — declared once, checked against a real sample before the bulk run.

WHY THIS EXISTS. The test-first gate enforced that a small sample RAN. It did not enforce that the sample's
output was usable: `test_job`'s verifier was optional, and omitting it recorded `verified=1`. So a full batch
could be authorized by a test that proved only that the API returned something. That is the same failure shape
as counting base64 as tokens and as a leak check that only watched one direction — a check that records DONE
rather than CORRECT, and therefore reports success while the thing it guards is broken.

The failure it actually catches is not "item 1 didn't parse" — you'd notice that. It is: item 1 parses, item 400
comes back with a sentence before the JSON, and nothing says a word until the whole batch is paid for.

FORMAT, NOT MEANING. This module decides whether output PARSES INTO THE DECLARED SHAPE — a mechanical question,
fully determined by the bytes. Whether an answer is *right* is a judgement and belongs to the agentic quality
path (`call_io` sampling + judging), never here. Keeping the two apart is deliberate: a format check that
quietly opined about correctness would be a regex standing in for a model.

DECLARING A CONTRACT — four forms, cheapest first:

    contract = ["patient_id", "findings"]        # required keys on a JSON object
    contract = "json"                            # must parse as JSON, shape unconstrained
    contract = {"type": "object", "required": [...], "properties": {"n": {"type": "number"}}}
    contract = lambda item: item["score"] > 0    # a callable: False or a raise = failure

STRICT vs SALVAGED. A model that wraps its JSON in a code fence, or writes a sentence first, is a REAL problem
for a batch pipeline — the downstream parser may or may not cope, and you will find out at item 400. So output
that only parses after stripping a fence/preamble is reported separately as `salvaged`, never silently counted
as clean. The caller decides whether to accept it; the gate records what actually happened.
"""
import hashlib
import json
import re

# The only regexes here are FORMAT extraction on a known convention (a fenced block, a JSON object literal) —
# never a judgement about what the text means.
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)\s*```", re.S)
_BRACES = re.compile(r"[\[{].*[\]}]", re.S)


class Result:
    """What a sample actually did. Counts, not a verdict — the caller (and the ledger) decide what passes."""

    def __init__(self, n=0):
        self.n = n
        self.parsed = 0            # matched the contract from the raw bytes
        self.salvaged = 0          # matched only after stripping a fence/preamble — a real risk, counted apart
        self.failed = 0
        self.failures = []         # [{index, reason}] — the first few, for the operator

    @property
    def clean(self):
        """Every item matched with no salvaging. The only state that should authorize a bulk run unattended."""
        return self.n > 0 and self.failed == 0 and self.salvaged == 0

    @property
    def first_failure(self):
        return self.failures[0]["reason"] if self.failures else ""

    def summary(self):
        bits = [f"{self.parsed}/{self.n} parsed"]
        if self.salvaged:
            bits.append(f"{self.salvaged} salvaged (fence/preamble — downstream parser may not cope)")
        if self.failed:
            bits.append(f"{self.failed} FAILED: {self.first_failure}")
        return " · ".join(bits)

    def as_dict(self):
        return {"n": self.n, "parsed": self.parsed, "salvaged": self.salvaged, "failed": self.failed,
                "clean": self.clean, "first_failure": self.first_failure}


def describe(contract):
    """A stable, human-readable identity for a contract — goes in the ledger so a CHANGED contract expires the
    test flag, exactly as a changed prompt already changes the sig. Callables are identified by qualname:
    we cannot hash a closure's behaviour, and pretending otherwise would be worse than admitting it."""
    if contract is None:
        return ""
    if callable(contract):
        # MODULE-QUALIFIED. Two modules each defining `validate` produced the identical identity, so one
        # contract's test flag satisfied the other's — the exact staleness this identity exists to expire.
        # The module is where a qualname stops being ambiguous, and it costs nothing to include.
        _n = getattr(contract, "__qualname__", None) or getattr(contract, "__name__", "fn")
        _mod = getattr(contract, "__module__", "") or ""
        return "callable:" + (f"{_mod}.{_n}" if _mod else _n)
    if isinstance(contract, str):
        return "parse:" + contract.strip().lower()
    if isinstance(contract, (list, tuple, set)):
        return "keys:" + ",".join(sorted(str(k) for k in contract))
    if isinstance(contract, dict):
        return "schema:" + json.dumps(contract, sort_keys=True, default=str)
    return "unknown:" + type(contract).__name__


def contract_hash(contract):
    d = describe(contract)
    return hashlib.sha256(d.encode("utf-8")).hexdigest()[:16] if d else ""


def _as_obj(item):
    """(value, salvaged) — the item as a Python value. Strings are parsed as JSON: strictly first, then by
    stripping a code fence or taking the outermost brace span. Anything that needed stripping is flagged."""
    if not isinstance(item, str):
        return item, False
    s = item.strip()
    try:
        return json.loads(s), False
    except Exception:
        pass
    for pat in (_FENCE, _BRACES):
        m = pat.search(s)
        if m:
            try:
                return json.loads(m.group(1) if pat is _FENCE else m.group(0)), True
            except Exception:
                continue
    raise ValueError("not JSON (and no JSON found inside a fence or braces)")


_JSON_TYPES = {"object": dict, "array": (list, tuple), "string": str, "number": (int, float),
               "integer": int, "boolean": bool, "null": type(None)}


_EMPTY_VALUES = (0, 0.0, "", [], {}, None, False)


def _is_empty(v):
    """A required field present as 0 / "" / [] / null is ABSENCE wearing a value. Strict schema enforcement
    guarantees the KEY exists — never that it means anything — which is how a reviewer returned
    `line_start: 0, line_end: 0` for every finding and passed every check. Same invariant as unpriced ≠ $0."""
    return any(v is e or v == e for e in _EMPTY_VALUES if type(v) is type(e) or v is e)


def _check_schema(obj, schema, path="$"):
    """JSON-Schema-lite: type / required / properties / items. Deliberately small — a full validator is a
    dependency, and this exists to answer 'will my parser cope', not to be a spec-complete implementation.
    Raises ValueError naming the exact path that failed."""
    t = schema.get("type")
    if t:
        want = _JSON_TYPES.get(t)
        if want is None:
            raise ValueError(f"{path}: unknown type {t!r} in contract")
        if t == "number" and isinstance(obj, bool):
            raise ValueError(f"{path}: expected number, got boolean")
        if not isinstance(obj, want):
            raise ValueError(f"{path}: expected {t}, got {type(obj).__name__}")
    for k in schema.get("required") or ():
        if not isinstance(obj, dict) or k not in obj:
            raise ValueError(f"{path}: missing required key {k!r}")
    # `nonempty` is the answer to required-and-present-but-meaningless. Declared per field, checked mechanically
    # (a value IS or is not 0/""/[]), never a judgement about whether the content is any good.
    for k in schema.get("nonempty") or ():
        if isinstance(obj, dict) and _is_empty(obj.get(k)):
            raise ValueError(f"{path}.{k}: present but EMPTY ({obj.get(k)!r}) — a required field returned as "
                             f"0/\"\"/[] is absence, not an answer")
    for k, sub in (schema.get("properties") or {}).items():
        if isinstance(obj, dict) and k in obj:
            _check_schema(obj[k], sub, f"{path}.{k}")
    if schema.get("items") and isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_schema(v, schema["items"], f"{path}[{i}]")


def check_item(item, contract):
    """(ok, salvaged, reason) for ONE item. Never raises — a contract that explodes on real data is itself the
    finding, and losing it inside a traceback would defeat the purpose."""
    try:
        if callable(contract):
            ok = bool(contract(item))              # called ONCE — a verifier may be expensive or stateful
            return ok, False, ("" if ok else "verifier returned False")
        obj, salvaged = _as_obj(item)
        if isinstance(contract, str):
            if contract.strip().lower() != "json":
                return False, salvaged, f"unknown contract {contract!r} (use 'json', keys, a schema, or a callable)"
            return True, salvaged, ""
        if isinstance(contract, (list, tuple, set)):
            if not isinstance(obj, dict):
                return False, salvaged, f"expected an object with keys, got {type(obj).__name__}"
            missing = [k for k in contract if k not in obj]
            return (not missing), salvaged, (f"missing key(s): {', '.join(map(str, missing))}" if missing else "")
        if isinstance(contract, dict):
            _check_schema(obj, contract)
            return True, salvaged, ""
        return False, salvaged, f"unsupported contract type {type(contract).__name__}"
    except Exception as e:
        return False, False, f"{type(e).__name__}: {e}"


def check(items, contract, max_failures=5):
    """Validate EVERY item of a sample against the contract. Checking only the first would miss precisely the
    failure this is for — the one that appears at item 400."""
    items = list(items or [])
    r = Result(len(items))
    if contract is None or not items:
        return r
    for i, it in enumerate(items):
        ok, salvaged, why = check_item(it, contract)
        if ok and salvaged:
            r.salvaged += 1
            r.parsed += 1
        elif ok:
            r.parsed += 1
        else:
            r.failed += 1
            if len(r.failures) < max_failures:
                r.failures.append({"index": i, "reason": why})
    return r


def data_signature(items):
    """A stable fingerprint of the INPUTS a test ran on — so a sig tested on three toy rows cannot authorize a
    run over the real corpus. Hashes only; the data itself never leaves the caller (this file stores nothing)."""
    h = hashlib.sha256()
    for it in (items or []):
        try:
            blob = it if isinstance(it, (str, bytes)) else json.dumps(it, sort_keys=True, default=str)
        except Exception:
            blob = repr(it)
        h.update((blob if isinstance(blob, bytes) else blob.encode("utf-8", "ignore")))
        h.update(b"\x1e")
    return h.hexdigest()[:16] if items else ""
