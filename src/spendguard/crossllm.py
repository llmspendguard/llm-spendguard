"""`spendguard.ask` — the ONE stable way to run a cross-LLM query, so external callers never reach into
vendor_call internals to fan out across models.

(The module is `crossllm` and the function is `ask`: a module named `ask` would be SHADOWED in the package
namespace by `from .crossllm import ask`, the exact module-vs-function collision the estimate/estimate_cost
note in __init__ exists to prevent. The public surface is the function `spendguard.ask`.)

This is the public surface over the primitives that already exist (`vendor_call.fan_out` / `first_ok` /
`consensus`) plus the two things every real cross-LLM caller re-implements badly:

  1. ESTIMATE-FIRST ADMISSION. Before firing, estimate the metered spend (lanes are $0 and excluded); if a
     `budget_usd` is given and the estimate exceeds it, REFUSE with the number instead of spending and finding
     out. Spend is a scheduling input, not just an after-the-fact record.
  2. AN HONEST RESULT. `AskResult` exposes the answers that came back, per-vendor coverage, and `complete` —
     and NEVER lets a truncated/empty/errored call be read as an answer (it inherits vendor_call's Result.text
     invariant). A caller cannot accidentally count a failure as a reviewer.

Concurrency/rate limiting is automatic: every call routes through the dispatch governor (dispatch.py), so
`ask` over many items at once queues per vendor/lane instead of thrashing. The caller does nothing for that.

    import spendguard
    r = spendguard.ask("Review this file for bugs.\n\n" + src,
                       vendors=["anthropic:claude-opus-4-8", "openai:gpt-5.5",
                                "moonshot:kimi-k3", "zai:glm-5.3"],
                       schema=FINDINGS_SCHEMA, budget_usd=0.50)
    if not r.complete:
        print("partial coverage:", r.by_vendor)     # honest — some vendors may have failed
    for text in r.answers:                          # only OK results; failures are never in here
        ...
"""
from . import vendor_call, pricing, gate

# Named defaults (a value with a name, overridable per call) — never a literal at a call site.
_DEFAULT_DEADLINE_S = 200.0        # per-vendor default when the caller names none; time_budget refines it per model
_BUDGET_EST_OUTPUT_TOKENS = 4000   # OUTPUT tokens, PRE-FLIGHT COST ESTIMATE ONLY — NOT a send-time max_tokens cap, and
                                   # NOT any input bound. The real call floors OUTPUT at adapters.TOKEN_FLOOR (32k) and the
                                   # ledger bills ACTUAL tokens; this figure only prices the fan-out before it runs.
_CHARS_PER_TOKEN = 4               # rough INPUT-token proxy for that same estimate; the real input count is metered downstream


class BudgetRefused(gate.SpendGateRefused):
    """The estimated metered cost of this fan-out exceeds the caller's `budget_usd`. Raised BEFORE any spend —
    the estimate-first rail, applied to a cross-LLM query. Carries the estimate and the breakdown.

    Subclasses SpendGateRefused ON PURPOSE: a budget refusal IS a deliberate spend refusal, so it must propagate
    through every fail-open / degrade handler by CONSTRUCTION (the doctrine in gate.SpendGateRefused), not by being
    remembered in an enumerated `except (…)` tuple that drifts. gate.deliberate_stop_types() therefore covers it
    automatically, and a handler catching SpendGateRefused now catches an over-budget refusal too."""

    def __init__(self, estimate, budget, detail):
        self.estimate, self.budget, self.detail = estimate, budget, detail
        # An unpriced metered vendor (detail value None) is refused for a DIFFERENT reason than an over-budget
        # estimate: the cost is UNKNOWN, not merely too high, so say so honestly rather than implying "$0 > budget".
        self.unpriced = [v for v, c in detail.items() if c is None]
        why = (f"UNKNOWN price for metered vendor(s) {self.unpriced} — the estimate cannot bound the spend"
               if self.unpriced else
               f"estimated metered cost ${estimate:.4f} exceeds budget ${budget:.4f}")
        super().__init__(
            f"{why} — refusing before spend (budget ${budget:.4f}). Per-vendor est: {detail}. "
            f"Raise budget_usd, drop the vendor, sync-prices for it, or route it to a $0 lane.")


class ModelPreflightRefused(gate.SpendGateRefused):
    """A named model id is NOT callable as written — STALE (renamed), UNPRICED, or an UNKNOWN provider — refused
    BEFORE any spend, the model-validity twin of BudgetRefused. A batch that would otherwise pay for a whole run of
    failures (the gemini-3-flash → gemini-3-flash-preview case) stops HERE with each bad id and its fix. Subclasses
    SpendGateRefused so it propagates through every fail-open handler by construction, and carries the preflight rows."""

    def __init__(self, bad_rows):
        self.bad = list(bad_rows)
        detail = "; ".join(f"{r.get('spec')}: {r.get('note')}" for r in self.bad)
        super().__init__(f"refusing before spend — {len(self.bad)} model id(s) not callable as written: {detail}")


def _preflight_or_refuse(vlist):
    """HARD GATE: every named model in `vlist` must be callable AS WRITTEN before we spend. Raises
    ModelPreflightRefused naming each stale/unpriced/unknown id + its fix if any is not usable, so a bad id is caught
    for ~$0 up front instead of after paying for a run of failures. Re-checked on EVERY call — NEVER memoized — so a
    mid-process change (a model revoked, renamed, or unpriced after an earlier clean call) is caught on the next call,
    not skipped on a stale cached OK. The re-check is cheap: served_check is cache-first ($0), a clean served id makes
    no agentic call, and a stale id stops the batch on the FIRST call (so there is never a per-item agentic cost). A
    DELIBERATE stop from the preflight itself (gate/budget/deadline) propagates untouched — it is not 'a bad model'."""
    from . import model_preflight
    specs = sorted(f"{v}:{m}" for v, m in vlist)
    rows = model_preflight.preflight_models(specs)
    bad = [r for r in rows if not r.get("usable")]
    if bad:
        raise ModelPreflightRefused(bad)


def _parse_vendors(vendors):
    """Accept ["vendor:model", ...] or [("vendor","model"), ...]; return [(vendor, model)]. A model id may itself
    contain ':' (some providers namespace) — only the FIRST ':' splits vendor from model."""
    out = []
    for v in vendors:
        if isinstance(v, (tuple, list)):
            if len(v) != 2:
                raise ValueError(f"vendor spec {v!r} must be (vendor, model)")
            out.append((str(v[0]).strip(), str(v[1]).strip()))
        elif isinstance(v, str) and ":" in v:
            vendor, model = v.split(":", 1)
            out.append((vendor.strip(), model.strip()))
        else:
            raise ValueError(f"vendor spec {v!r} must be 'vendor:model' or (vendor, model)")
    if not out:
        raise ValueError("ask() needs at least one vendor — pass vendors=['vendor:model', ...] or set a default "
                         "panel in config `ask.default_vendors` (env SPENDGUARD_ASK_DEFAULT_VENDORS)")
    return out


def _default_vendors():
    """The configured default panel ('vendor:model,vendor:model' in config `ask.default_vendors`, or env
    SPENDGUARD_ASK_DEFAULT_VENDORS), or []. NO hardcoded model ids — spendguard does not presume a model
    catalogue; a deployment names its own panel ONCE and then callers can omit `vendors` and just pick a count."""
    import os
    raw = os.environ.get("SPENDGUARD_ASK_DEFAULT_VENDORS")
    if raw is None:
        try:
            from . import config
            raw = config._cfg_get("ask", "default_vendors", None)
        except Exception:
            raw = None
    return [v.strip() for v in str(raw).split(",") if v.strip()] if raw else []


def _is_metered(vendor):
    """True when this vendor is NOT riding an active $0 subscription lane — i.e. the call actually bills.
    A lane ($0, kind=subscription) contributes nothing to the budget estimate."""
    try:
        from . import adapters
        return not adapters._lane_for((vendor or "").strip().lower())
    except Exception:
        return True                    # unknown → treat as metered (conservative: never under-count a budget)


def _estimate_metered(vlist, prompt, system, est_output_tokens):
    """(total_usd, {vendor:usd|None}, [unpriced_vendor]) — the zero-spend pre-flight estimate of the metered
    vendors only. Lanes are $0 and omitted; a metered vendor whose price can't be looked up gets detail None and
    is listed in `unpriced` (so a budget guard can refuse rather than read the gap as $0). Deliberately coarse
    and slightly high (a budget guard, not the ledger): a fixed output figure, a char/4 input proxy."""
    in_tok = (len(prompt or "") + len(system or "")) // _CHARS_PER_TOKEN
    out_tok = int(est_output_tokens or _BUDGET_EST_OUTPUT_TOKENS)
    detail, total, unpriced = {}, 0.0, []
    for vendor, model in vlist:
        if not _is_metered(vendor):
            detail[vendor] = 0.0
            continue
        try:
            raw = pricing.realtime_cost(model, in_tok, out_tok)
        except Exception:
            raw = None
        if raw is None:
            # UNKNOWN price on a metered vendor. Estimating it as $0 (the old `or 0.0`) would let a budget_usd
            # guard PASS and then spend without bound — an unverifiable cost is not a $0 cost. Mark it unpriced so
            # ask() refuses when a budget is set.
            unpriced.append(vendor)
            detail[vendor] = None
            continue
        c = float(raw)
        detail[vendor] = round(c, 6)
        total += c
    return round(total, 6), detail, unpriced


class AskResult:
    """The honest outcome of a cross-LLM query. Thin, read-only view over a vendor_call fan-out dict; every
    property that could leak a failure-as-answer is guarded by vendor_call's own Result.text invariant."""

    __slots__ = ("_fan", "mode", "estimate", "run_id")

    def __init__(self, fan, mode, estimate=None):
        self._fan, self.mode, self.estimate = fan, mode, estimate
        # STORED, not a getter. The run's id is DATA copied off the fan-out dict — deliberately an attribute,
        # not a run_id() method, so it never shares a bare CALLABLE name with vendor_call.run_id, which GENERATES
        # the process run id (NAME_REGISTRY: run_id=COLLISION — a reader and a generator are different jobs).
        self.run_id = fan.get("run_id")

    @property
    def complete(self):
        """True only when the run got what it asked for — all vendors (mode='all') or `need` of them
        (mode='first'). The single flag a caller must branch on; consensus() enforces it too."""
        return self._fan["complete"]

    @property
    def n(self):
        return self._fan["n"]

    @property
    def n_ok(self):
        return self._fan["n_ok"]

    @property
    def results(self):
        return list(self._fan["results"])

    @property
    def ok_results(self):
        """The OK Result objects (kind == 'ok'). Their `.text` is readable; failures are never in here. Named
        `ok_results`, not `ok`, so a list-of-results ACCESSOR never shares a bare name with Result.ok, the
        success BOOLEAN (NAME_REGISTRY: ok=COLLISION — a list getter and a predicate are different jobs)."""
        return list(self._fan["ok"])

    @property
    def failed(self):
        return list(self._fan["failed"])

    @property
    def answers(self):
        """Just the answer strings that actually came back. Safe by construction: only OK results carry text."""
        return [r.text for r in self._fan["ok"]]

    @property
    def by_vendor(self):
        """{vendor: kind} across every vendor asked — the coverage map. A caller reads this to see WHO failed
        and WHY (ok/truncated/empty/refused/transport_error/deadline_exceeded/schema_violation/unfunded)."""
        return {r.vendor: r.kind for r in self._fan["results"]}

    @property
    def cost(self):
        """Actual $ billed across this fan-out (lanes contribute $0), summed from the per-call Result costs."""
        return round(sum(float(r.cost or 0) for r in self._fan["results"]), 6)

    def consensus(self, require=None):
        """The OK results, but only if enough vendors answered THIS run — else raises NotOk. Defaults to all
        (mode='all') or `need` (mode='first'). This is the call that refuses a false N-of-N."""
        return vendor_call.consensus(self._fan, require=require)

    def as_dict(self):
        """Machine-readable and HONEST: answers carry text; failures carry kind + error, never text. This is
        what a CLI --json or an external service serializes — a failure can never serialize as an answer."""
        return {
            "run_id": self.run_id, "mode": self.mode, "complete": self.complete,
            "n": self.n, "n_ok": self.n_ok, "cost": self.cost, "estimate": self.estimate,
            "results": [
                ({"vendor": r.vendor, "model": r.model, "kind": r.kind, "text": r.text,
                  "cost": r.cost, "latency": round(r.latency, 2), "elapsed_s": r.elapsed_s,
                  "attempts": r.attempts, "run_id": r.run_id, "in_tok": r.in_tok, "out_tok": r.out_tok} if r.ok else
                 # FULL failure detail honestreview logs verbatim — http_status separates a 429/529 overload from a
                 # 400 rejection, provider_error carries the vendor's real reason, attempts/finish_reason/text_head
                 # say what happened. A failed vendor still NEVER carries `text` (no false success on the wire).
                 {"vendor": r.vendor, "model": r.model, "kind": r.kind, "error": r.error,
                  "http_status": r.http_status, "provider_error": r.provider_error,
                  "stop_reason": r.stop_reason, "finish_reason": r.finish_reason,
                  "attempts": r.attempts, "text_head": r.text_head, "in_tok": r.in_tok, "out_tok": r.out_tok,
                  "elapsed_s": r.elapsed_s, "latency": round(r.latency, 2), "run_id": r.run_id})
                for r in self._fan["results"]],
        }

    def __repr__(self):
        return f"<AskResult {self.n_ok}/{self.n} ok complete={self.complete} ${self.cost}>"


def ask(prompt, *, vendors=None, n=None, schema=None, system=None, purpose="ask", deadline_s=None,
        budget_usd=None, mode="all", require=None, max_tokens=None, est_output_tokens=None, preflight=True):
    """Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.

    vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default
                     panel (config `ask.default_vendors`) — then just pick a count with `n`.
    n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick
                     check, the full list for high stakes). The first `n` as ordered, so put must-haves first.
    schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer
    mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered
                     "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met
    budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend
    deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.
    max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used
    preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before
                     any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated
                     once per vendor-set per process. Pass preflight=False only when the caller already preflighted.

    Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error
    (bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written
    (ModelPreflightRefused) — both deliberate, both BEFORE any spend."""
    if vendors is None:
        vendors = _default_vendors()
    vlist = _parse_vendors(vendors)
    if n is not None:
        # THE CALLER PICKS HOW MANY LLMs. Deterministic — the first `n` as ordered, so the caller controls
        # priority (put the must-have models first). Fewer than asked is fine; more than the list uses all.
        k = max(1, int(n))
        if k < len(vlist):
            vlist = vlist[:k]
    if preflight:
        # HARD GATE, before the estimate and any spend: every named model must be callable AS WRITTEN (served +
        # priced). A stale/unpriced/unknown id is refused here with its fix (ModelPreflightRefused) instead of being
        # discovered after paying for a run of failures — the 'test the model before the full run' rail. Validated
        # once per vendor-set per process (memoized), so a 26-item batch checks once, not 26×.
        _preflight_or_refuse(vlist)
    estimate, detail, unpriced = _estimate_metered(vlist, prompt, system, est_output_tokens)
    if budget_usd is not None and (estimate > float(budget_usd) or unpriced):
        # Refuse on EITHER an over-budget estimate OR an unpriceable metered vendor: a vendor whose price we
        # cannot look up estimates to $0, and a $0 estimate would sail past any budget and then spend unbounded.
        raise BudgetRefused(estimate, float(budget_usd), detail)
    dl = float(deadline_s or _DEFAULT_DEADLINE_S)
    if mode == "first":
        fan = vendor_call.first_ok(vlist, prompt, deadline_s=dl, need=int(require or 1), purpose=purpose,
                                   system=system, schema=schema, max_tokens=max_tokens)
    elif mode == "all":
        fan = vendor_call.fan_out(vlist, prompt, deadline_s=dl, purpose=purpose, system=system,
                                  schema=schema, max_tokens=max_tokens)
    else:
        raise ValueError(f"mode must be 'all' or 'first', not {mode!r}")
    return AskResult(fan, mode, estimate=estimate)


def cmd(argv=None):
    """`spendguard ask` — run one prompt across models from the shell. Honest by construction: a non-ok vendor
    prints its FAILURE KIND, never text, and the exit code is 0 only when the run is complete."""
    import argparse
    import json as _json
    import sys as _sys
    ap = argparse.ArgumentParser(prog="spendguard ask",
                                 description="Run one prompt across multiple LLMs; honest per-vendor coverage.")
    ap.add_argument("prompt", nargs="?", help="the prompt text; omit to read from stdin")
    ap.add_argument("--vendors", help="comma-separated vendor:model (e.g. "
                                      "anthropic:claude-opus-4-8,openai:gpt-5.5,moonshot:kimi-k3,zai:glm-5.3); "
                                      "omit to use config ask.default_vendors")
    ap.add_argument("--n", type=int, default=None, help="use only this many of the vendors (the first N) — you pick the panel size")
    ap.add_argument("--schema", help="path to a JSON Schema the answer must satisfy")
    ap.add_argument("--system", help="system prompt")
    ap.add_argument("--purpose", default="ask", help="intent tag for attribution in the ledger")
    ap.add_argument("--deadline", type=float, default=None, help="per-vendor deadline seconds")
    ap.add_argument("--budget", type=float, default=None, help="refuse if the estimated metered $ exceeds this")
    ap.add_argument("--mode", choices=["all", "first"], default="all")
    ap.add_argument("--require", type=int, default=None, help="for --mode first: how many must answer")
    ap.add_argument("--json", action="store_true", help="emit the honest machine-readable result")
    a = ap.parse_args(argv)
    prompt = a.prompt if a.prompt is not None else _sys.stdin.read()
    if not (prompt or "").strip():
        print("ask: no prompt (pass an argument or pipe it on stdin)", file=_sys.stderr)
        return 2
    vendors = [v.strip() for v in a.vendors.split(",") if v.strip()] if a.vendors else None
    schema = None
    if a.schema:
        with open(a.schema) as fh:
            schema = _json.load(fh)
    try:
        r = ask(prompt, vendors=vendors, n=a.n, schema=schema, system=a.system, purpose=a.purpose,
                deadline_s=a.deadline, budget_usd=a.budget, mode=a.mode, require=a.require)
    except BudgetRefused as e:
        print(f"ask: {e}", file=_sys.stderr)
        return 1
    if a.json:
        print(_json.dumps(r.as_dict(), indent=2))
        return 0 if r.complete else 1
    print(f"\n{r.n_ok}/{r.n} answered · complete={r.complete} · ${r.cost} · est ${r.estimate} · run {r.run_id}")
    for res in r.results:
        if res.ok:
            print(f"\n── {res.vendor}:{res.model}  [OK {res.latency:.0f}s ${res.cost or 0:.4f}] ──")
            print(res.text)
        else:
            print(f"\n── {res.vendor}:{res.model}  [{res.kind.upper()}] "
                  f"{res.error or res.stop_reason or ''}".rstrip())
    return 0 if r.complete else 1
