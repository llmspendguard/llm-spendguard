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
        # IDENTITY IS THE CODE, NOT THE NAME. Two rounds of this:
        #   1. `__qualname__` alone — two modules each defining `validate` shared one identity, so one
        #      contract's test flag satisfied the other's.
        #   2. module + qualname — better, and still wrong for the commonest case in this codebase: every
        #      module-level lambda is `<lambda>`, so two DIFFERENT lambdas in one module remained
        #      indistinguishable. Found by re-validating the first fix against the fixed source, which is
        #      the whole point of re-reviewing rather than trusting that a fix landed.
        #
        # The compiled body settles it, and settles the other half too: an EDITED contract gets a new
        # identity and expires its test flag, which is exactly what this value is for — "a changed contract
        # expires the flag, as a changed prompt already changes the sig". A name cannot do that.
        _n = getattr(contract, "__qualname__", None) or getattr(contract, "__name__", "fn")
        _mod = getattr(contract, "__module__", "") or ""
        _code = getattr(contract, "__code__", None)
        _body = ""
        if _code is not None:
            _body = ":" + hashlib.sha256(
                getattr(_code, "co_code", b"") + repr(getattr(_code, "co_consts", ())).encode()
            ).hexdigest()[:12]
        else:
            # A callable OBJECT (functools.partial, a class instance): its type is the closest stable
            # identity available, and it is at least different for different types.
            _body = ":" + type(contract).__name__
        return "callable:" + (f"{_mod}.{_n}" if _mod else _n) + _body
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
    if isinstance(item, (bytes, bytearray)):
        # BYTES ARE A STRING THAT HASN'T BEEN DECODED, NOT AN OBJECT. `isinstance(item, str)` is False for
        # them, so a bytes payload was returned verbatim and every downstream contract check then compared
        # b'{"ok": true}' against a dict shape and reported the output as malformed. Providers and file
        # readers hand back bytes routinely, so this turned a perfectly valid response into a contract
        # failure — and a contract failure is what blocks a bulk run.
        try:
            item = item.decode("utf-8")
        except UnicodeDecodeError:
            return item, False        # genuinely not text: hand it back untouched rather than mangle it
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

# bool IS A SUBCLASS OF int IN PYTHON, so isinstance(True, int) is True and a field declared `integer` or
# `number` accepted True/False without complaint. A model answering `true` where a COUNT was required
# passed the contract and became 1 downstream — a validator that lets the wrong type through is worse than
# none, because the caller stops checking. JSON Schema treats them as distinct types and so does this.
_BOOL_EXCLUDED = ("integer", "number")


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
        # BOTH numeric types, not just one. This guard existed for "number" and never for "integer", so
        # the author had already met the bool-is-an-int gotcha and covered half of it — and `integer` is
        # the half a COUNT is declared as.
        if t in _BOOL_EXCLUDED and isinstance(obj, bool):
            raise ValueError(f"{path}: expected {t}, got boolean")
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


def check_items_against_contract(items, contract, max_failures=5):
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


def check_envelope(text, expected_ids, results_key="results", id_key="id"):
    """ARITY / completeness for an id-keyed batch envelope — the check `check_item` CANNOT do. A PACKED request
    sends N items and expects ONE envelope {results:[{id,…},…]} carrying EVERY id back exactly once. check_item
    validates each PRESENT item's shape; it is blind to an item that never came back — the model silently OMITS
    ids from results[] (measured ~7% on packed batches), and an envelope of N−1 shape-perfect items passes every
    per-item check. So the fallback decision (adapters `_shape_ok`) made on check_item alone cannot see the loss.

    This checks the ID SET, which is FORMAT (counting ids is mechanical, fully determined by the bytes), never
    meaning. Returns (ok, detail) where detail = {reason, missing, extra, dupes, n_expected, n_got}. `text` may be a
    JSON string/bytes or an already-parsed dict/list; a top-level list is accepted as the results array directly."""
    exp = [str(i) for i in (expected_ids or [])]
    try:
        obj, _sal = _as_obj(text) if isinstance(text, (str, bytes, bytearray)) else (text, False)
    except Exception as e:
        return False, {"reason": f"envelope did not parse: {type(e).__name__}: {e}",
                       "missing": exp, "extra": [], "dupes": [], "n_expected": len(exp), "n_got": 0}
    rows = obj.get(results_key) if isinstance(obj, dict) else obj      # a bare top-level list is the results array
    if not isinstance(rows, (list, tuple)):
        return False, {"reason": f"no '{results_key}' array in the envelope (got {type(rows).__name__})",
                       "missing": exp, "extra": [], "dupes": [], "n_expected": len(exp), "n_got": 0}
    from collections import Counter
    cnt = Counter(str(r.get(id_key)) for r in rows if isinstance(r, dict) and r.get(id_key) is not None)
    exp_set = set(exp)
    missing = [i for i in exp if i not in cnt]                        # sent but never came back
    extra = [g for g in cnt if g not in exp_set]                     # came back but never sent (hallucinated id)
    dupes = [g for g, c in cnt.items() if c > 1]                     # same id answered twice
    ok = not missing and not extra and not dupes
    reason = "" if ok else "; ".join(p for p in (
        (f"missing {len(missing)}: {missing[:5]}" if missing else ""),
        (f"extra {len(extra)}: {extra[:5]}" if extra else ""),
        (f"duplicated {len(dupes)}: {dupes[:5]}" if dupes else "")) if p)
    return ok, {"reason": reason, "missing": missing, "extra": extra, "dupes": dupes,
                "n_expected": len(exp), "n_got": len(cnt)}


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
