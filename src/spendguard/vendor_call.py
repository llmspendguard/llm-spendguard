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
import random
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
UNFUNDED = "unfunded"            # the account cannot pay — deterministic, actionable, and NOT retryable
OVERLOADED = "overloaded"        # 429/529 — the vendor is rate-limited/overloaded: TRANSIENT, retry (honor Retry-After)
PAYLOAD_REJECTED = "payload_rejected"  # 400/413/414/401/403 — bad/oversized/unauthenticated request: PERMANENT, never retried
KINDS = (OK, TRUNCATED, EMPTY, REFUSED, TRANSPORT_ERROR, DEADLINE_EXCEEDED, SCHEMA_VIOLATION, UNFUNDED,
         OVERLOADED, PAYLOAD_REJECTED)
FAILURES = tuple(k for k in KINDS if k != OK)
# Transient classes worth retrying: a broken connection (transport) or a vendor asking us to slow down
# (overloaded). Everything else is deterministic — a truncation, an empty body, a refusal, a bad payload, an
# unfunded account, a blown deadline — where a retry only spends the deadline and the money to get the same answer.
RETRYABLE = (TRANSPORT_ERROR, OVERLOADED)

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
                 "latency", "error", "prompt_sha", "run_id", "ts", "purpose", "payload",
                 "http_status", "provider_error", "attempts", "text_head")

    def __init__(self, kind, vendor, model, text=None, stop_reason=None, in_tok=0, out_tok=0, cost=None,
                 latency=0.0, error=None, prompt_sha="", purpose="", payload=None,
                 http_status=None, provider_error=None, attempts=1, text_head=None):
        if kind not in KINDS:
            raise ValueError(f"unknown result kind {kind!r} — one of {KINDS}")
        self.kind, self._text = kind, (text if kind == OK else None)
        self.vendor, self.model, self.stop_reason = vendor, model, stop_reason
        self.in_tok, self.out_tok, self.cost, self.latency = in_tok, out_tok, cost, latency
        self.error, self.prompt_sha, self.purpose, self.payload = error, prompt_sha, purpose, payload
        # FULL failure detail a caller can log verbatim (honestreview reads these): the HTTP status (separates a
        # 429/529 overload from a 400 rejection), the provider's own error BODY (the real reason, not str(e)), how
        # many attempts it took, and a peek at any (possibly partial) body — captured even for a non-ok result.
        self.http_status, self.provider_error, self.attempts = http_status, provider_error, int(attempts or 1)
        self.text_head = (text_head if text_head is not None else (text or ""))[:200]
        self.run_id, self.ts = run_id(), time.time()

    @property
    def ok(self):
        return self.kind == OK

    @property
    def elapsed_s(self):
        """honestreview's name for latency (seconds, wall-clock over every attempt)."""
        return round(self.latency, 3)

    @property
    def finish_reason(self):
        """honestreview's name for stop_reason (the vendor's own word for how the response ended)."""
        return self.stop_reason

    @property
    def text(self):
        if self.kind != OK:
            raise NotOk(f"{self.vendor}/{self.model}: result is {self.kind!r} "
                        f"(stop_reason={self.stop_reason!r}, error={self.error!r}) — there is no usable text. "
                        f"A {self.kind} response is a FAILURE; treating it as content is the bug this prevents.")
        return self._text

    def as_row(self):
        """The persisted shape — enough for a consumer to tell THIS run's answer from a previous one, AND to log a
        failure verbatim. Carries both spendguard's names (latency/stop_reason) and honestreview's (elapsed_s/
        finish_reason) so either consumer reads what it expects."""
        return {"run_id": self.run_id, "ts": self.ts, "vendor": self.vendor, "model": self.model,
                "purpose": self.purpose, "kind": self.kind, "stop_reason": self.stop_reason,
                "finish_reason": self.stop_reason, "prompt_sha": self.prompt_sha,
                "in_tok": self.in_tok, "out_tok": self.out_tok, "cost": self.cost,
                "latency": round(self.latency, 3), "elapsed_s": round(self.latency, 3), "error": self.error,
                "http_status": self.http_status, "provider_error": self.provider_error,
                "attempts": self.attempts, "text_head": self.text_head}

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


# Documented provider identifiers for "this account cannot pay". Same discipline as _POLICY_MARKERS: the
# provider's own error text on a 402/429, never an interpretation of arbitrary prose.
_BILLING_MARKERS = ("insufficient balance", "insufficient_quota", "insufficient credit", "no resource package",
                    "exceeded your current quota", "billing_hard_limit_reached", "payment required",
                    "credit balance is too low", "please recharge")


def _is_unfunded(err):
    """True only when the provider says the ACCOUNT cannot pay, on a status that means it.

    WHY THIS IS ITS OWN KIND. A 429 is normally rate-limiting — transient, and retrying is exactly right.
    But a 429 carrying "Insufficient balance ... Please recharge" is permanent until a human acts, and the
    two were the same bucket. MEASURED: z.ai returned it on all 10 files of a review wave. The panel retried
    each one, reported `transport_error`, and produced a run that looked like it had four reviewers while
    it had three — the findings gate then counted agreement out of a denominator that was quietly wrong.

    An unfunded vendor is also the one failure a person can FIX in thirty seconds, and it was being
    reported in the same words as a network blip nobody can do anything about."""
    if not err:
        return False
    t = str(err).lower()
    if not any(x in t for x in ("402", "429", "payment", "quota", "balance", "billing")):
        return False
    return any(m in t for m in _BILLING_MARKERS)


def _is_policy_rejection(err):
    """True only when the provider's own error names a policy decision. Returns False on anything ambiguous:
    mislabelling a transport blip as a refusal would suppress the retry that fixes it."""
    if not err:
        return False
    t = str(err).lower()
    if "error code: 4" not in t and "400" not in t and "403" not in t:
        return False                              # 5xx / timeouts / connection resets are transport, always
    return any(m in t for m in _POLICY_MARKERS)


# Exception CLASS names (a structured signal, never message prose) that mean the vendor did not ANSWER within
# the budget — a DEADLINE — as opposed to the connection breaking or being refused, which stays a transport
# fault. The SDKs wrap an httpx timeout as APITimeoutError; a raw httpx timeout surfaces its own class.
_DEADLINE_EXC_TYPES = ("APITimeoutError", "ReadTimeout", "PoolTimeout", "WriteTimeout", "TimeoutException",
                       "TimeoutError", "Timeout")


def _retry_after_s(v):
    """A provider's Retry-After header → seconds to wait, or None. FORMAT parsing on a fixed contract: either an
    integer number of seconds, or an HTTP-date. When present it is the vendor telling us exactly how long to hold
    off on a 429/529, so it takes precedence over our own backoff."""
    if v is None:
        return None
    s = str(v).strip()
    try:
        return max(0.0, float(s))                         # the common form: a number of seconds
    except ValueError:
        pass
    try:
        import email.utils as _eut
        dt = _eut.parsedate_to_datetime(s)
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except Exception:
        pass
    return None


def _classify(r, want_text=True):
    """(kind, stop_reason) from an adapters result. Parsing declared fields, not inferring intent."""
    # AN EXPLICIT TRUNCATION IS A TRUNCATION, NOT A TRANSPORT FAULT — even though adapters attaches an `error`
    # string to it after raising the cap and re-asking (retries exhausted). Labeling it TRUNCATED rather than
    # transport_error is what keeps fan_out's coverage report honest about WHY the vendor didn't answer, and —
    # with Result.text raising on TRUNCATED — is the last guard that stops a cut-off body being read as "no
    # findings" (measured: a truncated kimi review scored as a clean/empty result, a coverage lie).
    if r.get("truncated") is True:
        return TRUNCATED, r.get("finish_reason")
    if r.get("error"):
        # A DEADLINE IS NOT A TRANSPORT FAULT. The vendor connected and simply did not answer within the budget
        # (the join abandoned it, or the client read-timeout fired) — distinct from the connection breaking.
        # Told apart by the exception TYPE adapters preserved (error_type), or the abandon flag from _attempt —
        # never the message prose. This is what lets fan_out's coverage report distinguish "too slow" from
        # "unreachable" instead of one opaque transport_error, and (unlike transport) it is NOT retried: the
        # budget that was going to be spent already has been.
        if r.get("deadline") or (r.get("error_type") in _DEADLINE_EXC_TYPES):
            return DEADLINE_EXCEEDED, r.get("finish_reason")
        # A PROVIDER-SIDE POLICY REJECTION IS A REFUSAL, NOT A TRANSPORT FAULT. The distinction is not
        # cosmetic: transport faults are RETRIED, and a deterministic policy rejection retried three times
        # is three times the latency and three times nothing. Measured on Moonshot — a source-code payload
        # over ~16,000 chars returns HTTP 400 "the request was rejected because it was considered high
        # risk" (param: prompt), every time, and the retries changed nothing.
        # This reads the provider's own structured error fields (an HTTP status and a documented error
        # identifier), which is FORMAT parsing on a fixed contract. It is deliberately conservative: an
        # unrecognised 400 stays a transport error rather than being talked into a category.
        if _is_unfunded(r.get("error")):
            # Checked FIRST: it arrives on a 429, which the transport path would otherwise retry forever.
            return UNFUNDED, "account_cannot_pay"
        if _is_policy_rejection(r.get("error")):
            return REFUSED, "policy_rejection"
        # STATUS-CODE taxonomy — a structured signal from the provider, PARSED not interpreted. Unfunded 429s and
        # policy 4xx are already split off above; the rest splits by code:
        #   429 / 529     → OVERLOADED       — rate-limited / overloaded: TRANSIENT, retry (honoring Retry-After).
        #   400/413/414 (bad or oversized request) · 401/403 (auth) → PAYLOAD_REJECTED — PERMANENT: retrying an
        #     oversized prompt or a bad key changes nothing, so it must NOT ride the transport retry loop and burn
        #     the deadline (measured: Moonshot rejects a >~16k-char prompt with a 400, every time).
        # An unknown/absent status stays TRANSPORT_ERROR (a reset / other 5xx) — deliberately conservative.
        st = r.get("status_code")
        if st in (429, 529):
            return OVERLOADED, r.get("finish_reason")
        if st in (400, 401, 403, 413, 414):
            return PAYLOAD_REJECTED, r.get("finish_reason")
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
         attempts=3, backoff_s=2.0, reasoning=None):
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
        # THE GUARD MAY NOT DEMAND MORE THAN THE ADVISOR IS ALLOWED TO PROPOSE. `time_budget` clamps every
        # proposal to DEADLINE_CEIL_S, so a class whose p95 exceeds the ceiling could satisfy neither: the advisor
        # returned the ceiling and this check rejected the ceiling, making the class permanently unservable with
        # ZERO attempts — which surfaces to the caller as a transport error, not as the arithmetic it is. A p95
        # above the hard ceiling is a statement that this class cannot be completed within the bound we are willing
        # to wait; the honest response is to run it AT the bound and let a genuinely slow call hit the deadline
        # (where it is recorded, censored from the percentiles, and sets a floor), not to refuse to try.
        _need = min(_need, DEADLINE_CEIL_S)
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
    # INPUT BOUND, checked before a byte is sent, and INDEPENDENT of the output budget: this bounds the INPUT by
    # the model's INPUT window (max_input_tokens) and never touches max_tokens (set separately, below, from the
    # OUTPUT axis — a big input does not shrink the reply, nor a big reply the allowed input). The provider will
    # reject an over-window request anyway; the point is to fail here, naming the two numbers, instead of paying
    # for a round trip and reading a provider error that does not say which field was too big. Same rail as the
    # estimate-side impossibility check, and the SAME accurate, image-aware token count the gate uses (so a
    # base64 image is not miscounted as its raw text length, and a prose doc that fits is not false-refused by a
    # chars/4 over-count) — a per-request input above the published context window is a physical bound.
    try:
        from . import pricing
        window = pricing.max_input_tokens(model)
        text = (system or "") + "\n" + (prompt or "")
        try:
            from .gate import _content_tokens
            approx_in = _content_tokens(text, provider=vendor, model=model)
        except Exception:
            approx_in = len(text) // 4      # rail fallback if the tokenizer is unavailable — never leaves the window unchecked
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
        # A CALLER-SUPPLIED OUTPUT CAP IS VALIDATED, NEVER TRUSTED. max_tokens is the OUTPUT axis — independent of
        # the input-window bound above: it sizes the REPLY and is never affected by how large the input was. This
        # was the hole: `max_tokens is None` took the measured path, and anything else was passed straight
        # through. A literal below what this class demonstrably produces buys nothing (billing is on tokens
        # GENERATED) and destroys the answer.
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
    # ATTRIBUTION AT THE CHOKEPOINT. `purpose` was recorded on every vendor_call result (400/400) and reached
    # the LEDGER on none of them: 533 of 535 calls in a $25.29 session had no intent, so the money could be
    # totalled and not explained. The information existed the whole time and never got to the consumer.
    #
    # The `caller` column made it worse, not better: fan_out runs vendors on a thread pool, so 450 calls and
    # $24.66 were attributed to `threading.py:run:1024` — the worker frame, not the work. Concurrency added
    # for speed silently destroyed the only attribution there was.
    #
    # Tagging here covers every caller at once, which is the point of having a chokepoint.
    _ctx = None
    if purpose:
        try:
            from . import calls as _calls
            _ctx = _calls.context(intent=purpose, chain=run_id())
            _ctx.__enter__()
        except Exception:
            _ctx = None
    started = time.time()
    # DISPATCH ADMISSION — bound in-flight calls per vendor/lane (and optional RPM), so a caller fanning out
    # over many items QUEUES instead of thrashing the subprocess lanes or 429-storming a metered vendor. The
    # queue wait counts against THIS deadline; timing out in the queue is an honest DEADLINE_EXCEEDED, never a
    # silent success. It sits at the same chokepoint as attribution, so it covers every caller at once. dispatch.py.
    _dispatched = False
    try:
        from . import dispatch
        dispatch.acquire(vendor, model, deadline_s)
        _dispatched = True
    except dispatch.DispatchTimeout as _dt:
        if _ctx is not None:
            try:
                _ctx.__exit__(None, None, None)
            except Exception:
                pass
        return Result(DEADLINE_EXCEEDED, vendor, model, prompt_sha=_sha(prompt), purpose=purpose,
                      latency=time.time() - started, error=str(_dt))
    except Exception:
        _dispatched = False                          # governor unavailable → proceed ungoverned, never block a call
    sha = _sha(prompt)
    last = None
    # EVERY ATTEMPT BILLS. Returning only the final attempt's cost made retries invisible to the caller's
    # budget, so a hard stop could not stop anything — the run that found this thought it had spent $1.98
    # while the ledger recorded $13.12. The Result now carries what the CALL cost, not what its last try cost.
    billed = 0.0
    del cap_basis                       # recorded via the caps registry; not part of the result contract
    try:
        for attempt in range(1, max(1, int(attempts)) + 1):
            remaining = deadline_s - (time.time() - started)
            if remaining <= 0:
                return Result(DEADLINE_EXCEEDED, vendor, model, prompt_sha=sha, purpose=purpose,
                              latency=time.time() - started,
                              error=f"total deadline {deadline_s}s exhausted after {attempt - 1} attempt(s)")
            r = _attempt(vendor, model, prompt, system, max_tokens, remaining, schema=schema,
                         reasoning=reasoning)
            billed += float(r.get("cost") or 0.0)
            kind, stop = _classify(r)
            last = Result(kind, vendor, model, text=r.get("text"), stop_reason=stop,
                          in_tok=r.get("in_tok") or 0, out_tok=r.get("out_tok") or 0,
                          cost=(billed if billed else r.get("cost")),
                          latency=time.time() - started, error=r.get("error"), prompt_sha=sha, purpose=purpose,
                          http_status=r.get("status_code"), provider_error=r.get("provider_error"),
                          attempts=attempt)
            # Retry only the TRANSIENT classes (a broken connection, or a 429/529 overload). A truncation, empty
            # body, refusal, PAYLOAD_REJECTED (a 400 on an oversized prompt), unfunded account, or blown deadline
            # is deterministic — repeating it just burns the deadline and the money for the same answer.
            if kind not in RETRYABLE or attempt >= attempts:
                break
            # Backoff = base·2^(attempt-1) with FULL JITTER (sleep a uniform fraction, so N callers retrying one
            # vendor's 429 don't re-collide in lockstep). An overload's Retry-After, when the vendor sends one,
            # is a FLOOR — it is the vendor saying exactly how long to wait. Never sleep past the total deadline.
            ra = _retry_after_s(r.get("retry_after"))
            if ra is not None:
                wait = ra + random.uniform(0.0, backoff_s)           # honor the vendor's floor + a little de-sync
            else:
                wait = random.uniform(0.0, backoff_s * (2 ** (attempt - 1)))
            wait = min(wait, max(0.0, deadline_s - (time.time() - started)) - 0.05)
            if wait > 0:
                time.sleep(wait)
    finally:
        if _dispatched:
            try:
                from . import dispatch as _drel
                _drel.release(vendor, model)
            except Exception:
                pass
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
            bulkgate.note_latency(class_sig(model, purpose), model, last.latency, in_chars=len(prompt or ""),
                                  hit_deadline=(last.kind == DEADLINE_EXCEEDED))
        except Exception:
            pass
    if _ctx is not None:
        try:
            _ctx.__exit__(None, None, None)
        except Exception:
            pass
    if last is not None and last.ok and schema is not None:
        last = _apply_schema(last, schema)
    _persist(last)
    return last


def _attempt(vendor, model, prompt, system, max_tokens, budget_s, schema=None, reasoning=None):
    """One attempt, hard-bounded at `budget_s`. adapters.call has no timeout of its own, so the bound is
    enforced HERE by running it on a worker and abandoning it — a deadline checked only between attempts
    cannot bound a call that never returns, which is exactly what produced the 3h30m run."""
    from . import adapters
    box = {}
    # CARRY THE CONTEXT ACROSS THE THREAD BOUNDARY. calls.record_call() reads intent/chain from a THREAD-LOCAL
    # context, and this runs the adapter on a worker — so the tag is set on the calling thread and the ledger
    # row is written on another one, with an empty context. "Thread-local; safe under ThreadPool" is true for
    # isolation and precisely wrong for propagation: 533 of 535 calls in a $25.29 session were recorded with
    # no intent for exactly this reason, and the `caller` column showed `threading.py:run:1024` — the worker
    # frame — because the stack walk also happens on the wrong thread.
    try:
        from . import calls as _c
        _parent_ctx = dict(_c.current() or {})
    except Exception:
        _parent_ctx = {}

    def _run():
        if _parent_ctx:
            try:
                from . import calls as _c2
                _c2.set_context(intent=_parent_ctx.get("intent"), chain=_parent_ctx.get("chain"))
            except Exception:
                pass
        try:
            box["r"] = adapters.call(model if ":" in model else f"{vendor}:{model}", prompt,
                                     max_tokens=int(max_tokens), system=system,
                                     schema=schema if isinstance(schema, dict) else None,
                                     timeout_s=budget_s, reasoning=reasoning,
                                     no_substitution=True)   # vendor_call NAMES a vendor — the lane bandit must
                                     #                         never swap it (a panel/adjudication would collapse)
        except Exception as e:                      # adapters says it never raises; believe it, verify anyway
            box["r"] = {"error": f"{type(e).__name__}: {e}", "text": None}

    t = threading.Thread(target=_run, daemon=True)   # daemon: an abandoned call must never hold the process
    t.start()
    t.join(timeout=budget_s)
    if t.is_alive():
        # deadline=True: this is the ABANDON the module was named for — the call ran the whole budget without
        # returning. _classify maps it to DEADLINE_EXCEEDED, not transport_error, and call() does not retry it.
        return {"error": f"no response within {budget_s:.0f}s (abandoned)", "text": None,
                "finish_reason": None, "deadline": True}
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
        # raw-write-ok: a LOCKFILE, not data — ephemeral, unlinked on exit, and rebuilt trivially. Routing
        # it through update_json would be wrong in both directions: os.replace would clobber a lock another
        # process just took, and `~` backups of a pid file are noise. (Its check-then-write is racy under a
        # true concurrent start; that is a separate fix, not one to make silently while tidying writers.)
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


def _dispatch_form(mid):
    """A listed id in the DISPATCH form a caller actually requests. Gemini's /models lists ids as
    `models/gemini-3.7-flash`, while a request uses the bare `gemini-3.7-flash`; without stripping that fixed
    listing prefix, `serves()` compares a bare id against a prefixed list and reports every served Gemini model as
    ABSENT (measured: serves('gemini','gemini-3.7-flash') was False for a live model). A fixed-prefix strip —
    format normalisation, not a decision about meaning, and not a hardcoded model list."""
    s = str(mid or "")
    return s[len("models/"):] if s.startswith("models/") else s


def list_models(vendor, timeout_s=20):
    """What this vendor SERVES right now — {"vendor", "models": [{id, max_output_tokens?}], "error"}. Ids are the
    DISPATCH form (the string a request uses), so a caller can compare a requested id against this list directly.

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
        mid = _dispatch_form(mid)      # the DISPATCH form (strip Gemini's `models/` listing prefix) — a non-empty
        #                                id stays non-empty, so the guard above still owns the id-less case
        # Some OpenAI-compatible vendors expose limits on the listing; take them ONLY when present.
        lim = m.get("max_output_tokens") or m.get("max_tokens") or None
        out.append({"id": mid, "max_output_tokens": int(lim) if isinstance(lim, (int, float)) and lim else None})
    return {"vendor": vendor, "models": sorted(out, key=lambda r: r["id"]), "error": None}


def serves(vendor, model):
    """True/False/None — does this vendor serve this id RIGHT NOW? None when discovery itself failed, which is
    'we could not check', not 'no' (absence is unknown, never a verdict). LIVE (a /models GET); the pre-flight
    below reads a cache first and only calls this to CONFIRM a would-be rejection."""
    d = list_models(vendor)
    if d["error"]:
        return None
    ids = {m["id"] for m in d["models"]}
    return model in ids or model.split(":", 1)[-1] in ids


def served_check(vendor, model):
    """'served' | 'stale' | 'unchecked' for a DISPATCH pre-flight — the wired, cache-first form of serves().

    CACHE-FIRST, so the common path is $0 with NO per-call latency: the served-list cache (catalog, kept fresh by
    the sync cadence) answers a served id outright. A miss is never a rejection on its own — a stale cache must not
    cause a false 'stale', so a miss is CONFIRMED LIVE via serves() before it is ever called stale. And when there
    is no cached list at all for this vendor, the pre-flight stays DORMANT ('unchecked', pass through) rather than
    doing a live /models GET on the hot path — the sync cadence is what populates it. 'unchecked' is also what a
    live discovery failure yields: a can't-check is never turned into a 'no' (the same rule _input_fits states)."""
    from . import catalog
    ids = catalog.live_model_ids(vendor)               # cached served ids (dispatch form), or None if not cached
    if ids is None:
        return "unchecked"                             # no maintained list → dormant, no per-call live fetch
    if model in ids or model.split(":", 1)[-1] in ids:
        return "served"                                # $0 fast path — the fresh cache says served
    live = serves(vendor, model)                       # cache HAS the vendor but not this id → CONFIRM LIVE
    return "served" if live is True else ("unchecked" if live is None else "stale")


def closest_served(vendor, stale_model):
    """(same_model_id | None, live_ids) — the currently-served id that names the SAME model as a stale one, decided
    AGENTICALLY by pricing._same_model_as_ours (alias/version identity is a MEANING call — never a string-distance
    match), plus the live served list for context. Best-effort; any failure returns (None, live_ids or []). The
    agentic call is tiny and fires ONLY on the refusal path (a stale id), never on the $0 served fast-path — so the
    pre-flight itself stays $0 (a catalog read) and only a confirmed-stale dispatch pays for one identity judgement."""
    d = list_models(vendor)
    live = [m["id"] for m in d["models"]] if not d.get("error") else []
    if not live:
        return None, []
    try:
        from . import pricing
        resolved, _ = pricing._same_model_as_ours([stale_model], live, run=True, fact_key="served_id")
        return resolved.get(stale_model), live
    except Exception as e:
        # A DELIBERATE stop from the tiny identity call — the spend gate, a budget cap, or a drawn-on-purpose dispatch
        # deadline — must PROPAGATE, never be downgraded to 'uncorrectable' (which would let a caller keep spending
        # past the very cap that fired). The CONCEPT is named in one place; any other failure (transient/lookup) →
        # correction unknown, and the $0 served fast-path already passed so the caller still has the live list.
        from . import gate
        if isinstance(e, gate.deliberate_stop_types()):
            raise
        return None, live


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
        json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        pass
    # THROUGH THE ONE WRITER. This registry holds every MEASURED bound in the system — caps, input limits,
    # discovered effort tiers — each one paid for with real calls. `except: data = {}` then a full rewrite
    # meant a single unreadable byte threw all of them away and they would be silently re-measured.
    entry = {"max_output_tokens": int(max_output_tokens), "method": method, "source": source,
             "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    config.update_json(path, lambda d: d.update({f"{vendor}/{model}": entry}),
                       reason="record-cap", keep_backups=3)
    return entry


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
    # FORCE THE API PATH. A subscription lane (claude-code / codex) serves the prompt without ever sending
    # the provider's parameters, so every probe comes back clean and discovery concludes the endpoint
    # supports everything. Measured: gpt-5.5 was reported as accepting `minimal` when the codex lane had
    # answered it; the API rejects `minimal` with a 400. Third time in this project a lane has silently
    # invalidated a measurement — capability probes belong on the path whose capability is in question.
    import os as _os
    _prev = _os.environ.get("SPENDGUARD_ADVISOR_EXECUTOR")
    _os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    ok, rejected, unknown = [], [], []
    try:
        for eff in CANDIDATE_EFFORTS:
            r = adapters.call(model if ":" in model else f"{vendor}:{model}", "Reply: OK",
                              max_tokens=16, reasoning=eff, timeout_s=45, no_substitution=True)  # probe THIS vendor
            err = str(r.get("error") or "")
            # THE PARAMETER MAY HAVE BEEN DROPPED AND THE CALL RETRIED. That returns no error and looks exactly
            # like acceptance — it is how the first version of this probe reported that every vendor supported
            # every tier, including one that had rejected `auto` minutes earlier in a direct test.
            if "reasoning_effort" in (r.get("dropped") or []):
                rejected.append(eff)
            elif not err:
                ok.append(eff)
            else:
                # NO PROSE MATCH. This had `elif "effort" in err.lower(): rejected.append(eff)`, deciding
                # from the wording of an error message that the vendor had rejected this tier. Two ways that
                # is wrong, both silent: a rejection worded without the word "effort" ("invalid parameter:
                # reasoning") got filed as unknown, and any unrelated error that happened to mention effort
                # got filed as a REJECTION — which is a claim about vendor capability, recorded as fact.
                # The structured signal already exists: adapters records `dropped` when it identifies the
                # rejected parameter from the provider's typed `param` field, and that is the branch above.
                # Everything else is genuinely unknown, and unknown is the honest answer.
                unknown.append(eff)                    # transport/other/unattributable: evidence of nothing
    finally:
        if _prev is None:
            _os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)
        else:
            _os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = _prev
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
    config.update_json(path, lambda _d: data)


# WHERE EFFORT LIVES, and why there is no record_effort()/effort_policy() here any more.
#
# There were two registries. This module grew `record_effort` + `effort_policy` storing a chosen effort in
# the caps file, while `models.py` — which predates it and whose entire purpose is per-model facts that
# auto-apply — already had add_fact/profile. Nothing ever called the pair: the A/B correctly wrote its
# verdict via models.add_fact(), so the copy here sat dead, one plausible import away from being the place a
# future measurement got written and then never read. Two stores for one fact is worse than either store,
# because the failure is silent in whichever half you did not use.
#
# The split that remains is a real one, along the line between what an endpoint IS and what we CHOOSE:
#     caps registry (here)   MEASURED LIMITS of an endpoint — output_cap, input_limit, which effort tiers it
#                            ACCEPTS. Facts about the vendor, discovered by probing, true regardless of us.
#     models.py facts        WHAT WE SEND — the tier we picked, the tokens param, the cache mode. Decisions,
#                            with provenance, applied at the chokepoint by models.apply_call_params().
#
# discover_efforts() above answers "what does it accept" and belongs here. The A/B's verdict answers "what
# should we send" and belongs in models.py. Do not re-add a policy store to this file.


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
    config.update_json(path, lambda _d: data)
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
    """(tokens, basis) — the termination bound for this (vendor, model).

    THE FLOOR IS 32K UNLESS THE MODEL'S OWN PUBLISHED MAX IS LOWER. A measured registry/observed number only
    RAISES the cap above that floor, it never lowers it below — billing is on tokens GENERATED, so a floor costs
    nothing and only prevents truncation. Measured: kimi-k3's registry cap was a stale 26,128 (below the 32K the
    model finishes long reviews in), and because output_cap returned it as an explicit cap it bypassed the
    adapters TOKEN_FLOOR and starved the review. The RAISE precedence is recorded registry → this class's
    observed need; the result is then floored to 32K and clamped to the model's published max so it can never
    exceed what the endpoint will accept. There is no longer an 'unknown' return: the floor IS the default."""
    from . import adapters
    resolved, basis = 0, "floor"
    rec = caps().get(f"{vendor}/{model}")
    if rec and rec.get("max_output_tokens"):
        resolved, basis = int(rec["max_output_tokens"]), "registry:" + (rec.get("method") or "?")
    elif sig:
        try:
            from . import bulkgate
            b = bulkgate.maxtokens(sig)
            if b and b.get("recommend"):
                resolved, basis = int(b["recommend"]), "observed"
        except Exception:
            pass
    published = None
    try:
        from . import pricing
        published = pricing.max_output_tokens(model)
    except Exception:
        pass
    floor = min(adapters.TOKEN_FLOOR, int(published)) if published else adapters.TOKEN_FLOOR
    cap = max(resolved, floor)
    if published:
        cap = min(cap, int(published))
    return cap, basis


# A GUARD MUST NOT FIRE ON EVIDENCE TOO THIN TO BE EVIDENCE. Both bound-validators refuse a caller's number
# by citing a measurement, so there has to BE one: the first version refused deadline_s=1.0 against a "p95"
# derived from a SINGLE observation, which is not a distribution, it is an anecdote wearing a percentile's
# name. Matches the minimum time_budget already required before it will propose a number.
MIN_BOUND_OBS = 5

DEADLINE_SLACK = 2.0          # measured p99 x this. Slack for TIME, mirroring the cap's p99x1.5 for TOKENS.
DEADLINE_FLOOR_S = 30.0       # never propose a budget so tight that a healthy call cannot finish
# AND NEVER AN UNBOUNDED ONE — the 3h30m run is what this module exists to end. 600 -> 1800 because 600 sat BELOW
# measured p95s and thereby made real classes unservable: `time_budget` clamps its proposal to this ceiling while
# `call()` REFUSES any deadline under the class p95, so for a class measuring p95=1117s the advisor could only ever
# propose 600 and the guard could only ever reject it. Measured 2026-08-29/30: every vendor refused the first file
# of three warden stage reviews with BadBound at deadline_s=600 vs p95=1117 — zero attempts, and it read as a
# transport error. A ceiling below what work demonstrably takes is not bounding a hang, it is bounding the work.
DEADLINE_CEIL_S = 1800.0


def time_budget(vendor, model, sig=None, default_s=None, in_chars=None):
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
    # A SUBSCRIPTION LANE IS SLOWER THAN THE METERED PATH the latency was measured on — a CLI cold-start +
    # context injection runs 60-120s where the metered call is ~10s — so a metered-derived budget ABANDONS a
    # live lane call. MEASURED on a real panel: opus/gpt on the Claude Code / Codex lanes hit deadline_exceeded
    # at 40s/60s reviewing a bigger file, while zai's HTTP lane and metered kimi finished. When a lane is ACTIVE
    # for this vendor, floor the deadline at the lane minimum (mirrors adapters' lane subprocess/HTTP floor); the
    # lane is $0, so the extra wait costs latency, not money, and a hung lane is still bounded by its TIMEOUT_S.
    lane_floor = 0.0
    try:
        from . import adapters
        if adapters._lane_for(vendor):
            lane_floor = float(adapters.LANE_MIN_TIMEOUT_S)
    except Exception:
        pass
    for scope, kwargs in (("class", {"sig": sig, "model": model}), ("model", {"model": model})):
        if scope == "class" and not sig:
            continue
        # SIZE IS PASSED DOWN. Latency scales with the payload, so a budget drawn from a population of
        # mixed sizes is wrong at both ends: measured on kimi-k3's review calls, p50 was 38s and p95 289s,
        # and a budget from that mixture killed a 37,453-char file that genuinely needed 547s — losing its
        # review entirely while three other vendors reported on it. bulkgate narrows to comparable payloads
        # when it has enough of them and says which population it used.
        d = bulkgate.latency(near_chars=in_chars, **kwargs)
        if d and d.get("n", 0) >= 5 and d.get("p99"):
            want = float(d["p99"]) * DEADLINE_SLACK
            if d.get("floor"):
                # Calls already died at this budget: the work is demonstrably slower than anything we
                # completed, so the proposal can never sit below the budget that killed them.
                want = max(want, float(d["floor"]) * DEADLINE_SLACK)
            return max(DEADLINE_FLOOR_S, lane_floor, min(DEADLINE_CEIL_S, want)), f"measured:{scope}(n={d['n']})"
    if default_s:
        return max(float(default_s), lane_floor), "caller"
    if lane_floor:
        return lane_floor, "lane-floor"
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
        budget, _basis = time_budget(v, m, sig=class_sig(m, purpose), default_s=deadline_s, in_chars=len(prompt or ""))
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
        budget, _b = time_budget(v, m, sig=class_sig(m, purpose), default_s=deadline_s, in_chars=len(prompt or ""))
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
