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
import concurrent.futures as cf
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


class BadBound(ValueError):
    """A caller supplied a bound that MEASUREMENT says will destroy the call. Raised before anything is sent.

    This exists because the failure kept recurring and it was never a knowledge problem. Every call site is a
    fresh chance to type a number, and a wrong one does not announce itself: max_tokens=2000 on kimi-k3
    returned HTTP 200 with ZERO characters on 19 of 20 calls (reasoning consumed the budget), and a probe
    written hours after that lesson still hardcoded 600. Advice in a docstring loses to a literal at the call
    site, every time. So the library refuses the literal instead of describing why it is wrong.

    The way to never see this: do not pass a bound. Omit max_tokens and the measured one is used."""


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


# Documented provider identifiers for "we declined this request", checked against the structured error a
# provider returns. NOT an attempt to interpret arbitrary prose — an unmatched error stays a transport fault.
_POLICY_MARKERS = ("content_policy_violation", "content_filter", "invalid_prompt", "high risk",
                   "safety", "prohibited_content", "content_policy")


def _is_policy_rejection(err):
    """True only when the provider's own error names a policy decision. Returns False on anything ambiguous:
    mislabelling a transport blip as a refusal would suppress the retry that fixes it."""
    if not err:
        return False
    t = str(err).lower()
    if "error code: 4" not in t and "400" not in t and "403" not in t:
        return False                              # 5xx / timeouts / connection resets are transport, always
    return any(m in t for m in _POLICY_MARKERS)


def _classify(r, want_text=True):
    """(kind, stop_reason) from an adapters result. Parsing declared fields, not inferring intent."""
    if r.get("error"):
        # A PROVIDER-SIDE POLICY REJECTION IS A REFUSAL, NOT A TRANSPORT FAULT. The distinction is not
        # cosmetic: transport faults are RETRIED, and a deterministic policy rejection retried three times
        # is three times the latency and three times nothing. Measured on Moonshot — a source-code payload
        # over ~16,000 chars returns HTTP 400 "the request was rejected because it was considered high
        # risk" (param: prompt), every time, and the retries changed nothing.
        # This reads the provider's own structured error fields (an HTTP status and a documented error
        # identifier), which is FORMAT parsing on a fixed contract. It is deliberately conservative: an
        # unrecognised 400 stays a transport error rather than being talked into a category.
        if _is_policy_rejection(r.get("error")):
            return REFUSED, "policy_rejection"
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
    # THE DEADLINE IS VALIDATED TOO. max_tokens got this guard and deadline_s did not, and the asymmetry cost
    # a whole experiment: a probe passed deadline_s=150 against a class whose calls really take 56-116s, and
    # most results came back `deadline_exceeded` — which reads as a vendor failure and is a caller mistake.
    # A deadline below what the class demonstrably needs is the same defect as a cap below what it produces:
    # deterministic, self-inflicted, and paid for (the input bills whether or not you wait for the answer).
    try:
        from . import bulkgate
        _lat = bulkgate.latency(sig=class_sig(model, purpose), model=model)
        _need = (float(_lat.get("p95") or 0)
                 if _lat and (_lat.get("n") or 0) >= MIN_BOUND_OBS else 0.0)
        if _need and float(deadline_s) < _need:
            raise BadBound(
                f"{vendor}/{model}: deadline_s={float(deadline_s):.0f}s is below the measured p95 of "
                f"{_need:.0f}s for this call-class (n={_lat.get('n')}). The call would be abandoned after "
                f"the input was already billed. Omit it and use "
                f"vendor_call.time_budget({vendor!r}, {model!r}, sig=...), or pass a number above the "
                f"measurement deliberately.")
    except BadBound:
        raise
    except Exception:
        pass
    # INPUT BOUND, checked before a byte is sent. The provider will reject an over-window request anyway;
    # the point is to fail here, naming the two numbers, instead of paying for a round trip and reading a
    # provider error that does not say which field was too big. Same rail as the estimate-side impossibility
    # check — a per-request input above the published context window is a physical bound, not a threshold.
    try:
        from . import pricing
        window = pricing.max_input_tokens(model)
        approx_in = (len(prompt or "") + len(system or "")) // 4
        if window and approx_in > int(window):
            raise BadBound(
                f"{vendor}/{model}: this request's input is ~{approx_in:,} tokens but the model's context "
                f"window is {int(window):,}. The provider would reject it. Split the work, or use a model "
                f"with a larger window — never trim the prompt silently, which changes the task.")
    except BadBound:
        raise
    except Exception:
        pass

    cap_basis = "caller"
    if max_tokens is not None:
        # A CALLER-SUPPLIED CAP IS VALIDATED, NEVER TRUSTED. This was the hole: `max_tokens is None` took the
        # measured path, and anything else was passed straight through. A literal below what this class
        # demonstrably produces buys nothing (billing is on tokens GENERATED) and destroys the answer.
        try:
            from . import bulkgate
            b = bulkgate.maxtokens(class_sig(model, purpose))
            need = int(b.get("p95") or 0) if b and (b.get("n") or 0) >= MIN_BOUND_OBS else 0
            floor = int(b.get("recommend") or 0) if b else 0
            if need and int(max_tokens) < need:
                raise BadBound(
                    f"{vendor}/{model}: max_tokens={int(max_tokens):,} is below the measured p95 of "
                    f"{need:,} for this call-class (n={b.get('n')}, {b.get('truncations') or 0} truncation(s) "
                    f"already). A cap never controlled cost -- you are billed on tokens GENERATED -- and a "
                    f"low one turns a paid call into an unparseable body. Omit max_tokens and the measured "
                    f"bound ({floor or need:,}) is used, or pass that number deliberately.")
        except BadBound:
            raise
        except Exception:
            pass
    if max_tokens is None:
        # NOT a default constant. A 512 fallback would be the same invented number that returned zero
        # characters from two reasoning models — the registry answers this or nobody does.
        max_tokens, cap_basis = output_cap(vendor, model, sig=class_sig(model, purpose))
        if max_tokens is None:
            return Result(TRANSPORT_ERROR, vendor, model, prompt_sha=_sha(prompt), purpose=purpose,
                          error=f"no measured output cap for {vendor}/{model} and none supplied. Record one: "
                                f"vendor_call.record_cap('{vendor}', '{model}', <tokens>, method='probe'). "
                                f"Guessing it is what returned HTTP 200 with zero characters.")
    started = time.time()
    sha = _sha(prompt)
    last = None
    # EVERY ATTEMPT BILLS. Returning only the final attempt's cost made retries invisible to the caller's
    # budget, so a hard stop could not stop anything — the run that found this thought it had spent $1.98
    # while the ledger recorded $13.12. The Result now carries what the CALL cost, not what its last try cost.
    billed = 0.0
    del cap_basis                       # recorded via the caps registry; not part of the result contract
    for attempt in range(1, max(1, int(attempts)) + 1):
        remaining = deadline_s - (time.time() - started)
        if remaining <= 0:
            return Result(DEADLINE_EXCEEDED, vendor, model, prompt_sha=sha, purpose=purpose,
                          latency=time.time() - started,
                          error=f"total deadline {deadline_s}s exhausted after {attempt - 1} attempt(s)")
        r = _attempt(vendor, model, prompt, system, max_tokens, remaining, schema=schema)
        billed += float(r.get("cost") or 0.0)
        kind, stop = _classify(r)
        last = Result(kind, vendor, model, text=r.get("text"), stop_reason=stop,
                      in_tok=r.get("in_tok") or 0, out_tok=r.get("out_tok") or 0,
                      cost=(billed if billed else r.get("cost")),
                      latency=time.time() - started, error=r.get("error"), prompt_sha=sha, purpose=purpose)
        # Retry ONLY transport failures. A truncation or an empty body is a deterministic result: repeating it
        # burns the deadline and the money to arrive at the same answer.
        if kind != TRANSPORT_ERROR:
            break
        if attempt < attempts and (deadline_s - (time.time() - started)) > backoff_s:
            time.sleep(backoff_s * attempt)
    # Feed the TIME measurement on every outcome, the way note_response feeds the token one. A budget that is
    # only ever guessed can never improve; recorded, the next caller's deadline comes from what this vendor
    # actually does. Deadline hits are flagged so they are censored from the percentiles they would otherwise
    # drag downward — the ratchet that recommends ever-shorter deadlines the more calls it kills.
    # ONLY A CALL THAT DID THE WORK MEASURES THE WORK. A 400 comes back in under a second, and recording it
    # as a completed observation drags the class p99 toward zero — which shrinks the next budget, which makes
    # the next real call time out, which records another fast failure. A ratchet that tightens every time it
    # fires. Measured: 30 policy refusals on one class gave p50=0s, p99=10s and a budget of 30s (the floor),
    # against work that genuinely needs 15-30s per chunk. Deadline hits ARE recorded, censored, because they
    # set a floor: the work was at least that long. Every other failure is counted in the call log for the
    # reliability rate and contributes nothing to the timing distribution.
    if last is not None and (last.ok or last.kind == DEADLINE_EXCEEDED):
        try:
            from . import bulkgate
            bulkgate.note_latency(class_sig(model, purpose), model, last.latency,
                                  hit_deadline=(last.kind == DEADLINE_EXCEEDED))
        except Exception:
            pass
    if last is not None and last.ok and schema is not None:
        last = _apply_schema(last, schema)
    _persist(last)
    return last


def _attempt(vendor, model, prompt, system, max_tokens, budget_s, schema=None):
    """One attempt, hard-bounded at `budget_s`. adapters.call has no timeout of its own, so the bound is
    enforced HERE by running it on a worker and abandoning it — a deadline checked only between attempts
    cannot bound a call that never returns, which is exactly what produced the 3h30m run."""
    from . import adapters
    box = {}

    def _run():
        try:
            box["r"] = adapters.call(model if ":" in model else f"{vendor}:{model}", prompt,
                                     max_tokens=int(max_tokens), system=system,
                                     schema=schema if isinstance(schema, dict) else None,
                                     timeout_s=budget_s)
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


# ── E: the MEASURED output-cap registry ───────────────────────────────────────────────────────────────────
# max_tokens is a TERMINATION bound sized from measured need — not a cost control (billing is on tokens
# generated) and never a guess. The probe that motivated this found both reasoning models returning HTTP 200
# with ZERO characters at max_tokens=2000: the reasoning consumed the whole budget and `content` came back
# empty. kimi-k3 needed >= 26,128 and glm-5.2 >= 30,069 against the 2,000 sent. Nobody would have guessed that.
_CAP_FILE = "output_caps.json"
CAP_UNKNOWN = None


def _caps_path():
    return config.HOME / _CAP_FILE


def record_cap(vendor, model, max_output_tokens, method, source=""):
    """Store a measured cap WITH its provenance. `method` says how it was obtained ('probe' · 'vendor-docs' ·
    '/models') and the date is stamped here — a number without those is a guess wearing a registry's clothes."""
    if not max_output_tokens or int(max_output_tokens) <= 0:
        raise ValueError("a cap must be positive: zero or absent is the failure this registry exists to end")
    path = _caps_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    data[f"{vendor}/{model}"] = {"max_output_tokens": int(max_output_tokens), "method": method,
                                 "source": source, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                time.gmtime())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return data[f"{vendor}/{model}"]


# The candidate tiers to PROBE. Not a per-model decision and not a claim about what any model supports —
# just the set of values worth trying, seeded from what providers have themselves told us (z.ai replied
# "must be one of: none, minimal, low, medium, high"; kimi-k3 additionally accepts "auto"). Add to it and
# discovery picks the new value up everywhere; nothing here is keyed to a model id.
CANDIDATE_EFFORTS = ("none", "minimal", "low", "medium", "high", "auto")


def discover_efforts(vendor, model, refresh=False):
    """Which reasoning-effort values does this endpoint ACCEPT? PROBED, recorded, never hardcoded.

    Why probe rather than parse the provider's error text: the enumeration z.ai returns is a gift, but its
    FORMAT is the provider's and will drift, and every provider words it differently. Trying a value and
    seeing whether it is accepted is the same question answered empirically, works for any endpoint, and
    cannot rot into a regex that silently stops matching.

    Nearly free: a rejected parameter is refused before inference, so it bills nothing, and an accepted one
    is a two-token prompt. The result is cached in the caps registry with its provenance, so the probe runs
    once per (vendor, model) rather than once per call."""
    from . import adapters
    # THE STRUCTURAL FACT COMES FIRST, BEFORE ANY CACHE. A provider whose request shape never carries this
    # parameter cannot support it, and that is knowable without probing — so it must also OVERRIDE a stale
    # cached answer. The first version checked the cache first and kept serving anthropic's incorrect
    # "supports everything", recorded by the buggy probe, long after the probe was fixed. A wrong cached
    # capability that outlives its fix is worse than no cache.
    kind = (adapters.PROVIDERS.get(vendor) or {}).get("kind")
    if kind != "openai":
        out = {"accepted": [], "rejected": [], "not_applicable": True, "method": "request-shape",
               "note": f"{vendor} uses the {kind} request shape, which has no reasoning_effort parameter",
               "measured": time.strftime("%Y-%m-%d")}
        _write_efforts(vendor, model, out)     # overwrite whatever was cached, including a wrong answer
        return out
    rec = caps().get(f"{vendor}/{model}") or {}
    if not refresh and rec.get("efforts"):
        return rec["efforts"]
    ok, rejected, unknown = [], [], []
    for eff in CANDIDATE_EFFORTS:
        r = adapters.call(model if ":" in model else f"{vendor}:{model}", "Reply: OK",
                          max_tokens=16, reasoning=eff, timeout_s=45)
        err = str(r.get("error") or "")
        # THE PARAMETER MAY HAVE BEEN DROPPED AND THE CALL RETRIED. That returns no error and looks exactly
        # like acceptance — it is how the first version of this probe reported that every vendor supported
        # every tier, including one that had rejected `auto` minutes earlier in a direct test.
        if "reasoning_effort" in (r.get("dropped") or []):
            rejected.append(eff)
        elif not err:
            ok.append(eff)
        elif "effort" in err.lower():
            rejected.append(eff)
        else:
            unknown.append(eff)                    # transport/other: evidence of nothing
    if not ok and not rejected:
        return {}                                  # discovery itself failed: we know nothing, and say so
    out = {"accepted": ok, "rejected": rejected, "unknown": unknown, "method": "probe",
           "measured": time.strftime("%Y-%m-%d")}
    _write_efforts(vendor, model, out)
    return out


def _write_efforts(vendor, model, out):
    path = _caps_path()
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    data.setdefault(f"{vendor}/{model}", {})["efforts"] = out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def record_effort(vendor, model, effort, method, source="", sig=None):
    """A MEASURED effort for a call-class, with how it was established. `sig` scopes it to a call-class;
    without one it is the model-wide default. Mirrors record_cap: a number without provenance is a guess
    wearing a registry's clothes."""
    if not effort:
        raise ValueError("an effort must be named: absent is not 'minimal', it is unmeasured")
    path = _caps_path()
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    rec = data.setdefault(f"{vendor}/{model}", {})
    slot = rec.setdefault("effort_by_sig" if sig else "effort", {} if sig else None)
    entry = {"effort": effort, "method": method, "source": source, "measured": time.strftime("%Y-%m-%d")}
    if sig:
        slot[sig] = entry
    else:
        rec["effort"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return entry


def effort_policy(vendor, model, sig=None):
    """(effort, basis) — how hard this call should think. The THIRD bound, and the only one that was still
    being set by hand after max_tokens and the deadline were measured.

    Precedence, measured first, and NEVER an invented default:
      measured:class   an A/B on THIS call-class settled it
      measured:model   an A/B on this model settled it
      unmeasured       (None) -> send nothing, let the vendor decide

    `None` is the honest answer until a class has been measured. Sending "minimal" everywhere looked like a
    saving and was measured to destroy the work: glm-5.2 reviewing calls.py at minimal returned 10 output
    tokens and ZERO findings, where high returned 793 and found the real bug. Effort is also not monotonic —
    kimi-k3 found MORE at minimal on pricing.py than at high — so it cannot be reasoned about from the
    outside at all. It has to be run."""
    rec = caps().get(f"{vendor}/{model}") or {}
    if sig:
        e = (rec.get("effort_by_sig") or {}).get(sig)
        if e and e.get("effort"):
            return e["effort"], f"measured:class({e.get('method')})"
    e = rec.get("effort")
    if isinstance(e, dict) and e.get("effort"):
        return e["effort"], f"measured:model({e.get('method')})"
    return None, "unmeasured"


def record_input_limit(vendor, model, max_chars, method, source="", applies_to="any"):
    """A MEASURED input ceiling, with what it applies to and how it was found.

    Separate from the published context window, which is a different bound: this records a limit the provider
    enforces for its own reasons. Measured on Moonshot by bisection — source-code payloads pass at 15,500
    chars and are refused at 17,000 with "considered high risk", while PROSE of 40,000 chars passes fine. So
    the ceiling is not a size limit and not a content filter but the interaction of the two, which is exactly
    why it has an `applies_to` and why it is recorded rather than remembered."""
    if not max_chars or int(max_chars) <= 0:
        raise ValueError("a limit must be positive: zero or absent is the failure this registry exists to end")
    path = _caps_path()
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    key = f"{vendor}/{model}"
    rec = data.get(key) or {}
    rec.setdefault("input_limits", {})[applies_to] = {
        "max_chars": int(max_chars), "method": method, "source": source,
        "measured": time.strftime("%Y-%m-%d")}
    data[key] = rec
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return rec["input_limits"][applies_to]


def input_limit(vendor, model, applies_to="any"):
    """The recorded limit for this kind of payload, or None. None means UNMEASURED, never unlimited."""
    rec = caps().get(f"{vendor}/{model}") or {}
    return (rec.get("input_limits") or {}).get(applies_to)


def caps():
    try:
        return json.loads(_caps_path().read_text())
    except Exception:
        return {}


def class_sig(model, purpose):
    """The ONE way a call-class is identified here, so lookups cannot miss what recording wrote.

    They did. `call()` passed the raw purpose string ("panel:review") to output_cap, which forwards it to
    bulkgate.maxtokens(sig) — but note_response STORES observations under bulkgate.sig(model, template_id=...),
    a hash. The two never matched, so output_cap's middle rung, "this class's own observed need", never fired
    once: every call silently fell through to the published ceiling or to `unknown`. A measured system whose
    measurement is looked up under the wrong key is indistinguishable from one that never measured anything,
    and it fails quietly in the direction of guessing."""
    from . import bulkgate
    return bulkgate.sig(model, template_id=purpose or None)


def output_cap(vendor, model, sig=None):
    """(tokens, basis) — the termination bound for this (vendor, model). Precedence, all MEASURED or PUBLISHED,
    never guessed: the recorded registry → this class's own observed need → the vendor's published ceiling →
    (None, 'unknown'), which callers must treat as 'do not send a cap you invented'."""
    rec = caps().get(f"{vendor}/{model}")
    if rec and rec.get("max_output_tokens"):
        return int(rec["max_output_tokens"]), "registry:" + (rec.get("method") or "?")
    if sig:
        try:
            from . import bulkgate
            b = bulkgate.maxtokens(sig)
            if b and b.get("recommend"):
                return int(b["recommend"]), "observed"
        except Exception:
            pass
    try:
        from . import pricing
        lim = pricing.max_output_tokens(model)
        if lim:
            return int(lim), "vendor-published"
    except Exception:
        pass
    return CAP_UNKNOWN, "unknown"


# A GUARD MUST NOT FIRE ON EVIDENCE TOO THIN TO BE EVIDENCE. Both bound-validators refuse a caller's number
# by citing a measurement, so there has to BE one: the first version refused deadline_s=1.0 against a "p95"
# derived from a SINGLE observation, which is not a distribution, it is an anecdote wearing a percentile's
# name. Matches the minimum time_budget already required before it will propose a number.
MIN_BOUND_OBS = 5

DEADLINE_SLACK = 2.0          # measured p99 x this. Slack for TIME, mirroring the cap's p99x1.5 for TOKENS.
DEADLINE_FLOOR_S = 30.0       # never propose a budget so tight that a healthy call cannot finish
DEADLINE_CEIL_S = 600.0       # and never an unbounded one — the 3h30m run is what this module exists to end


def time_budget(vendor, model, sig=None, default_s=None):
    """(seconds, basis) — how long this (vendor, model[, class]) is ALLOWED to take, sized from measurement.

    THE EXACT TWIN OF output_cap(). A deadline is a termination bound for TIME, and every lesson from the token
    bound transfers unchanged:
      * one global number is always wrong for somebody — measured p90 across four vendors on identical prompts
        ranged 20.6s (openai) to 116.8s (moonshot), and both harnesses had hardcoded 180s for all of them;
      * too LOW destroys the call after you have already paid for the input (deadline_exceeded is 100% waste,
        exactly like truncation), so it is never a cost control;
      * too HIGH means waiting three minutes to learn a vendor is down;
      * a call that HIT the budget measures the budget, not the work, so it is censored from the percentiles
        and sets a floor instead.
    Precedence, measured first: this class on this model -> this model anywhere -> the caller's own number ->
    (None, "unknown"), which the caller must answer for rather than have a guess invented for them."""
    from . import bulkgate
    for scope, kwargs in (("class", {"sig": sig, "model": model}), ("model", {"model": model})):
        if scope == "class" and not sig:
            continue
        d = bulkgate.latency(**kwargs)
        if d and d.get("n", 0) >= 5 and d.get("p99"):
            want = float(d["p99"]) * DEADLINE_SLACK
            if d.get("floor"):
                # Calls already died at this budget: the work is demonstrably slower than anything we
                # completed, so the proposal can never sit below the budget that killed them.
                want = max(want, float(d["floor"]) * DEADLINE_SLACK)
            return max(DEADLINE_FLOOR_S, min(DEADLINE_CEIL_S, want)), f"measured:{scope}(n={d['n']})"
    if default_s:
        return float(default_s), "caller"
    return None, "unknown"


def fan_out(vendors, prompt, *, deadline_s, purpose="", system=None, schema=None, max_tokens=None):
    """Ask N vendors the same question. Returns {"results": [...], "ok": [...], "failed": [...], "n": N,
    "n_ok": k, "complete": k == N, "run_id": ...}.

    `complete` is the whole point. The harness that started this reported "45 findings from 4 reviewers" when
    zero of the four had answered, because the merge step read absence as success. A caller must branch on
    `complete` — and `consensus()` below refuses outright rather than trusting them to remember."""
    # CONCURRENT, not sequential. This was a list comprehension, so a four-vendor panel cost the SUM of four
    # latencies rather than the slowest one — measured p90s of 20.6 + 116.8 + 24.0 + 180.0s means a review
    # panel took over five minutes to do twenty seconds of parallel work, and the slowest vendor set the pace
    # for every question asked. Each vendor gets its own MEASURED budget: one global deadline is generous for
    # the fast vendor and marginal for the slow one at the same time.
    def _one(v, m):
        budget, _basis = time_budget(v, m, sig=class_sig(m, purpose), default_s=deadline_s)
        return call(v, m, prompt, deadline_s=budget or deadline_s, purpose=purpose, system=system,
                    schema=schema, max_tokens=max_tokens)

    with cf.ThreadPoolExecutor(max_workers=max(1, len(vendors))) as pool:
        futs = {pool.submit(_one, v, m): (v, m) for v, m in vendors}
        results = []
        for fut in cf.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:                     # a thread that died is a FAILED vendor, not a missing one
                v, m = futs[fut]
                results.append(Result(TRANSPORT_ERROR, v, m, error=f"{type(e).__name__}: {e}", purpose=purpose))
    ok = [r for r in results if r.ok]
    return {"results": results, "ok": ok, "failed": [r for r in results if not r.ok],
            "n": len(results), "n_ok": len(ok), "complete": len(ok) == len(results) and bool(results),
            "run_id": run_id()}


def first_ok(vendors, prompt, *, deadline_s, need=1, purpose="", system=None, schema=None, max_tokens=None):
    """Ask all vendors at once; return as soon as `need` of them have ANSWERED. For timeliness, not agreement.

    fan_out waits for everybody, so its latency is the SLOWEST vendor's — and one vendor timing out at 180s
    makes every question take 180s no matter how fast the other three were. When the job only needs an answer
    (a classification, an extraction), waiting on the straggler buys nothing. Measured: z.ai exceeded a 180s
    deadline on 9 of 40 calls while openai answered the same prompts with a p90 of 20.6s.

    Returns the same dict shape as fan_out plus `waited_for`, and `complete` still means need was MET — a
    caller that reads absence as success is the failure the whole module exists to prevent. Stragglers keep
    running to completion in the background so their latency and usage are still recorded: abandoning a call
    you already paid for teaches the estimator nothing."""
    got, results = [], []

    def _one(v, m):
        budget, _b = time_budget(v, m, sig=class_sig(m, purpose), default_s=deadline_s)
        return call(v, m, prompt, deadline_s=budget or deadline_s, purpose=purpose, system=system,
                    schema=schema, max_tokens=max_tokens)

    pool = cf.ThreadPoolExecutor(max_workers=max(1, len(vendors)))
    futs = {pool.submit(_one, v, m): (v, m) for v, m in vendors}
    t0 = time.time()
    try:
        for fut in cf.as_completed(futs, timeout=deadline_s):
            try:
                r = fut.result()
            except Exception as e:
                v, m = futs[fut]
                r = Result(TRANSPORT_ERROR, v, m, error=f"{type(e).__name__}: {e}", purpose=purpose)
            results.append(r)
            if r.ok:
                got.append(r)
                if len(got) >= max(1, int(need)):
                    break
    except cf.TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False)                      # stragglers finish and self-record; we stop WAITING
    return {"results": results, "ok": got, "failed": [r for r in results if not r.ok],
            "n": len(vendors), "n_ok": len(got), "need": int(need),
            "complete": len(got) >= max(1, int(need)),
            "waited_for": round(time.time() - t0, 1), "run_id": run_id()}


def consensus(fan, require=None):
    """The ok results, ONLY if enough vendors answered in THIS run. Raises otherwise — because the failure
    being prevented is a report that says N-of-N when it had k. `require` defaults to all of them."""
    need = (fan.get("need") or len(fan["results"])) if require is None else int(require)
    if fan["n_ok"] < need:
        raise NotOk(
            "%d of %d vendors answered in run %s — refusing to report a %d-vendor result. Failures: %s"
            % (fan["n_ok"], fan["n"], fan["run_id"], need,
               ", ".join(f"{r.vendor}/{r.model}={r.kind}" for r in fan["failed"]) or "none"))
    return fan["ok"]
