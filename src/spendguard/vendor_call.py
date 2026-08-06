"""ONE gated entry point for calling any vendor — with a TYPED outcome, a TOTAL deadline, and a fan-out that
cannot report a consensus it did not get.

Every rule here comes from a measured failure, not a preference:

  • A review harness ran 3h30m. kimi and glm raised APIConnectionError, opus and gpt-5.5 returned nothing, and
    the merge step republished the previous run byte-identically (45,401 bytes) while logging "45 findings from
    4 reviewers". Absence read as success, one level above the reviewer.
  • max_tokens at 8k truncated silently; at 4k returned HTTP 200 with zero characters and was logged as a
    success; removed entirely, kimi generated toward a 1,048,576-token ceiling until it timed out.
  • max_retries=3 with a long per-attempt timeout produced that 3h30m run: a deadline checked only BETWEEN
    attempts cannot bound a call that never returns.

THE CENTRAL INVARIANT: it is structurally impossible for a truncated or empty response to reach a caller as a
successful one. `ok` is the only kind carrying text, and it is only ever constructed after both checks pass.
That is enforced by construction — `Result.text` raises on any other kind — not by remembering to check.

WHAT THIS DOES NOT DO: it does not construct vendor clients. `adapters` already owns that, along with key
resolution, subscription lanes, and per-call metering. This is the typed, bounded, recorded shell around it —
extending what exists rather than growing a second client layer to drift from the first.
"""
import hashlib
import json
import os
import threading
import time

from . import config

# The kinds. Exactly one is a success; the rest are failures with a reason attached.
OK = "ok"
TRUNCATED = "truncated"          # the model was cut off — an incomplete JSON body parses to nothing
EMPTY = "empty"                  # HTTP 200, zero characters. The failure that logged as success.
REFUSED = "refused"              # the model declined (content policy, safety stop)
TRANSPORT_ERROR = "transport_error"
DEADLINE_EXCEEDED = "deadline_exceeded"
SCHEMA_VIOLATION = "schema_violation"
KINDS = (OK, TRUNCATED, EMPTY, REFUSED, TRANSPORT_ERROR, DEADLINE_EXCEEDED, SCHEMA_VIOLATION)
FAILURES = tuple(k for k in KINDS if k != OK)

_RUN_ID = None
_lock = threading.RLock()


class NotOk(RuntimeError):
    """Raised when a caller reads `.text` off a result that is not `ok`. The point is that this CANNOT be
    ignored: there is no path from a truncated or empty response to a string a caller can use by accident."""


class Result:
    """The outcome of one call. INVARIANT: `.text` is readable only when `kind == 'ok'`; every other kind raises
    on access. `.ok` is the boolean to branch on, and `.stop_reason` carries the vendor's own word for it."""

    __slots__ = ("kind", "_text", "vendor", "model", "stop_reason", "in_tok", "out_tok", "cost",
                 "latency", "error", "prompt_sha", "run_id", "ts", "purpose", "payload")

    def __init__(self, kind, vendor, model, text=None, stop_reason=None, in_tok=0, out_tok=0, cost=None,
                 latency=0.0, error=None, prompt_sha="", purpose="", payload=None):
        if kind not in KINDS:
            raise ValueError(f"unknown result kind {kind!r} — one of {KINDS}")
        self.kind, self._text = kind, (text if kind == OK else None)
        self.vendor, self.model, self.stop_reason = vendor, model, stop_reason
        self.in_tok, self.out_tok, self.cost, self.latency = in_tok, out_tok, cost, latency
        self.error, self.prompt_sha, self.purpose, self.payload = error, prompt_sha, purpose, payload
        self.run_id, self.ts = run_id(), time.time()

    @property
    def ok(self):
        return self.kind == OK

    @property
    def text(self):
        if self.kind != OK:
            raise NotOk(f"{self.vendor}/{self.model}: result is {self.kind!r} "
                        f"(stop_reason={self.stop_reason!r}, error={self.error!r}) — there is no usable text. "
                        f"A {self.kind} response is a FAILURE; treating it as content is the bug this prevents.")
        return self._text

    def as_row(self):
        """The persisted shape — enough for a consumer to tell THIS run's answer from a previous one."""
        return {"run_id": self.run_id, "ts": self.ts, "vendor": self.vendor, "model": self.model,
                "purpose": self.purpose, "kind": self.kind, "stop_reason": self.stop_reason,
                "prompt_sha": self.prompt_sha, "in_tok": self.in_tok, "out_tok": self.out_tok,
                "cost": self.cost, "latency": round(self.latency, 3), "error": self.error}

    def __repr__(self):
        return f"<Result {self.kind} {self.vendor}/{self.model} out={self.out_tok} {self.latency:.1f}s>"


def run_id():
    """A stable id for THIS process's run. Every result carries it, which is what lets a merge step tell a
    fresh answer from one written by an earlier run — the check whose absence republished a stale file."""
    global _RUN_ID
    if _RUN_ID is None:
        with _lock:
            if _RUN_ID is None:
                _RUN_ID = "run-%d-%s" % (int(time.time()), os.urandom(3).hex())
    return _RUN_ID


def _sha(s):
    return hashlib.sha256((s or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _classify(r, want_text=True):
    """(kind, stop_reason) from an adapters result. Parsing declared fields, not inferring intent."""
    if r.get("error"):
        return TRANSPORT_ERROR, r.get("finish_reason")
    fr = (r.get("finish_reason") or "").lower()
    txt = r.get("text")
    if fr in ("length", "max_tokens"):
        return TRUNCATED, r.get("finish_reason")
    if fr in ("content_filter", "refusal", "safety"):
        return REFUSED, r.get("finish_reason")
    if want_text and not (txt or "").strip():
        # HTTP 200, zero characters. This is the one that logged as success and read as "no findings".
        return EMPTY, r.get("finish_reason")
    return OK, r.get("finish_reason")


def call(vendor, model, prompt, *, deadline_s, purpose="", system=None, max_tokens=None, schema=None,
         attempts=3, backoff_s=2.0):
    """Call ONE model, bounded by a TOTAL deadline, returning a typed Result. Never raises for a call failure.

    INPUT invariant:  deadline_s > 0 and bounds the WHOLE call including every retry.
    OUTPUT invariant: a Result whose `.text` is readable ONLY when kind == 'ok'.

    `max_tokens` is a TERMINATION bound, not a cost control (billing is on tokens generated). It is sized from
    measurement — `expected_output`/`bulkgate.maxtokens` — never guessed, and never silently zero.
    """
    if not deadline_s or deadline_s <= 0:
        raise ValueError("deadline_s is required and must be > 0: an unbounded call is how a 3h30m run happens")
    started = time.time()
    sha = _sha(prompt)
    last = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        remaining = deadline_s - (time.time() - started)
        if remaining <= 0:
            return Result(DEADLINE_EXCEEDED, vendor, model, prompt_sha=sha, purpose=purpose,
                          latency=time.time() - started,
                          error=f"total deadline {deadline_s}s exhausted after {attempt - 1} attempt(s)")
        r = _attempt(vendor, model, prompt, system, max_tokens, remaining)
        kind, stop = _classify(r)
        last = Result(kind, vendor, model, text=r.get("text"), stop_reason=stop,
                      in_tok=r.get("in_tok") or 0, out_tok=r.get("out_tok") or 0, cost=r.get("cost"),
                      latency=time.time() - started, error=r.get("error"), prompt_sha=sha, purpose=purpose)
        # Retry ONLY transport failures. A truncation or an empty body is a deterministic result: repeating it
        # burns the deadline and the money to arrive at the same answer.
        if kind != TRANSPORT_ERROR:
            break
        if attempt < attempts and (deadline_s - (time.time() - started)) > backoff_s:
            time.sleep(backoff_s * attempt)
    if last is not None and last.ok and schema is not None:
        last = _apply_schema(last, schema)
    _persist(last)
    return last


def _attempt(vendor, model, prompt, system, max_tokens, budget_s):
    """One attempt, hard-bounded at `budget_s`. adapters.call has no timeout of its own, so the bound is
    enforced HERE by running it on a worker and abandoning it — a deadline checked only between attempts
    cannot bound a call that never returns, which is exactly what produced the 3h30m run."""
    from . import adapters
    box = {}

    def _run():
        try:
            box["r"] = adapters.call(model if ":" in model else f"{vendor}:{model}", prompt,
                                     max_tokens=int(max_tokens) if max_tokens else 512, system=system)
        except Exception as e:                      # adapters says it never raises; believe it, verify anyway
            box["r"] = {"error": f"{type(e).__name__}: {e}", "text": None}

    t = threading.Thread(target=_run, daemon=True)   # daemon: an abandoned call must never hold the process
    t.start()
    t.join(timeout=budget_s)
    if t.is_alive():
        return {"error": f"no response within {budget_s:.0f}s (abandoned)", "text": None,
                "finish_reason": None}
    return box.get("r") or {"error": "adapter returned nothing", "text": None}


def _apply_schema(res, schema):
    """Validate an ok result against the caller's declared shape. A violation is a FAILURE carrying the
    offending payload — never a silent {}."""
    from . import output_contract
    ok, _salvaged, why = output_contract.check_item(res._text, schema)
    if ok:
        return res
    bad = Result(SCHEMA_VIOLATION, res.vendor, res.model, stop_reason=res.stop_reason, in_tok=res.in_tok,
                 out_tok=res.out_tok, cost=res.cost, latency=res.latency, error=why,
                 prompt_sha=res.prompt_sha, purpose=res.purpose, payload=res._text)
    return bad


def _log_path():
    return str(config.HOME / "vendor_calls.jsonl")


def _persist(res):
    """Append the call record. Freshness is the point: every row carries the run_id and wall time, so a merge
    step can prove an answer came from THIS run rather than republishing a previous one."""
    if res is None:
        return
    try:
        with _lock, open(_log_path(), "a") as fh:
            fh.write(json.dumps(res.as_row()) + "\n")
    except Exception:
        pass


class JobLock:
    """Per-(repo, job) exclusion so two runs cannot clobber each other's output. Stale locks (dead pid) are
    reclaimed; a live one is refused rather than silently shared."""

    def __init__(self, job, repo=None):
        from . import budget
        self.name = f"{(repo or budget._project() or 'repo')}:{job}"
        self.path = config.HOME / ("lock-" + hashlib.sha256(self.name.encode()).hexdigest()[:12] + ".json")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text())
                os.kill(int(held["pid"]), 0)                    # signal 0 = "does this pid exist?"
                raise RuntimeError(f"{self.name} is already running (pid {held['pid']}, started "
                                   f"{held.get('started')}). Two runs would clobber each other's output.")
            except (OSError, ValueError, KeyError):
                pass                                            # stale lock from a dead process — reclaim it
        self.path.write_text(json.dumps({"pid": os.getpid(), "started": time.time(), "run": run_id()}))
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except Exception:
            pass
        return False


# Default API roots for the two vendors whose SDKs carry them implicitly (adapters records base_url=None for
# those). Named, not scattered: discovery needs a URL even when the SDK would have supplied one.
_DEFAULT_ROOT = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}
_ANTHROPIC_VERSION = "2023-06-01"        # the version header their REST API requires


def list_models(vendor, timeout_s=20):
    """What this vendor SERVES right now — {"vendor", "models": [{id, max_output_tokens?}], "error"}.

    INPUT invariant:  a vendor key present in adapters.PROVIDERS with a resolvable key.
    OUTPUT invariant: `models` lists ids the vendor itself returned. NEVER a guess — the whole point is that a
    caller stops hardcoding an id it has not confirmed exists (a guessed one 404'd).
    Free: a GET against /models. No tokens, no generation, no spend."""
    import urllib.request
    from . import adapters
    spec = adapters.PROVIDERS.get(str(vendor).strip().lower())
    if not spec:
        return {"vendor": vendor, "models": [], "error": f"unknown vendor {vendor!r}"}
    key = config.api_key(spec["key_env"])
    if not key:
        return {"vendor": vendor, "models": [], "error": f"no key ({spec['key_env']})"}
    root = (spec.get("base_url") or _DEFAULT_ROOT.get(spec["kind"]) or "").rstrip("/")
    if not root:
        return {"vendor": vendor, "models": [], "error": "no base_url and no default root for this kind"}
    hdr = ({"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION} if spec["kind"] == "anthropic"
           else {"Authorization": "Bearer " + key})
    try:
        req = urllib.request.Request(root + "/models", headers=hdr)
        body = json.loads(urllib.request.urlopen(req, context=config.ssl_context(), timeout=timeout_s).read())
    except Exception as e:
        return {"vendor": vendor, "models": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
    rows = body.get("data") if isinstance(body, dict) else body
    out = []
    for m in (rows or []):
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        # Some OpenAI-compatible vendors expose limits on the listing; take them ONLY when present.
        lim = m.get("max_output_tokens") or m.get("max_tokens") or None
        out.append({"id": mid, "max_output_tokens": int(lim) if isinstance(lim, (int, float)) and lim else None})
    return {"vendor": vendor, "models": sorted(out, key=lambda r: r["id"]), "error": None}


def serves(vendor, model):
    """True/False/None — does this vendor serve this id RIGHT NOW? None when discovery itself failed, which is
    'we could not check', not 'no' (absence is unknown, never a verdict)."""
    d = list_models(vendor)
    if d["error"]:
        return None
    ids = {m["id"] for m in d["models"]}
    return model in ids or model.split(":", 1)[-1] in ids


def fan_out(vendors, prompt, *, deadline_s, purpose="", system=None, schema=None, max_tokens=None):
    """Ask N vendors the same question. Returns {"results": [...], "ok": [...], "failed": [...], "n": N,
    "n_ok": k, "complete": k == N, "run_id": ...}.

    `complete` is the whole point. The harness that started this reported "45 findings from 4 reviewers" when
    zero of the four had answered, because the merge step read absence as success. A caller must branch on
    `complete` — and `consensus()` below refuses outright rather than trusting them to remember."""
    results = [call(v, m, prompt, deadline_s=deadline_s, purpose=purpose, system=system, schema=schema,
                    max_tokens=max_tokens) for v, m in vendors]
    ok = [r for r in results if r.ok]
    return {"results": results, "ok": ok, "failed": [r for r in results if not r.ok],
            "n": len(results), "n_ok": len(ok), "complete": len(ok) == len(results) and bool(results),
            "run_id": run_id()}


def consensus(fan, require=None):
    """The ok results, ONLY if enough vendors answered in THIS run. Raises otherwise — because the failure
    being prevented is a report that says N-of-N when it had k. `require` defaults to all of them."""
    need = len(fan["results"]) if require is None else int(require)
    if fan["n_ok"] < need:
        raise NotOk(
            "%d of %d vendors answered in run %s — refusing to report a %d-vendor result. Failures: %s"
            % (fan["n_ok"], fan["n"], fan["run_id"], need,
               ", ".join(f"{r.vendor}/{r.model}={r.kind}" for r in fan["failed"]) or "none"))
    return fan["ok"]
