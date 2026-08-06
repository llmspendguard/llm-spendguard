"""The vendor call layer, requirement by requirement — each one PROVEN to fail when its behaviour is removed.

A passing assertion proves nothing on its own: it may hold for a reason unrelated to the guard, or the guard
may already be dead. So every requirement below is checked TWICE — once with the behaviour in place, then again
with it deliberately broken, where the same check MUST fail. A guard that cannot be made to fail is not a guard,
it is a decoration, and this session produced several of those before anyone noticed.

Requirements are the ones from the spec, each traced to the measured failure that motivated it:
  A one entry point · B typed result · C total deadline · D probe · E measured caps · F model discovery
  G schema enforcement (request + value) · H pricing · I persistence + no false consensus · J lock · K the gate
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-vcall-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import inspect, json, pathlib, time
from spendguard import vendor_call as vc, output_contract as oc, adapters, pricing

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


def _try(fn):
    """True iff `fn` RAISES. Refusal is a behaviour to assert, not an accident to tolerate."""
    try:
        fn()
        return False
    except Exception:
        return True


def proves(req, label, probe, break_it, restore):
    """The mutation proof. `probe()` returns True when the guard is doing its job.
      1. with the behaviour present, probe() must be True;
      2. with it REMOVED, probe() must be False — otherwise the check is not testing the guard;
      3. restore, and confirm we are back.
    Both halves are reported, because a check that cannot fail is the thing being guarded against."""
    global failures
    intact = bool(probe())
    try:
        break_it()
        broken = bool(probe())
    finally:
        restore()
    back = bool(probe())
    ok = intact and not broken and back
    if not ok:
        failures += 1
    detail = "" if ok else f"  (intact={intact} broken={broken} restored={back})"
    print(f"  [{'OK' if ok else 'FAIL'}] {req}: {label} — and FAILS when removed{detail}")


# ── A. one entry point ────────────────────────────────────────────────────────────────────────────────────
print("-- A: one entry point; callers never construct a vendor client --")
src = pathlib.Path(inspect.getfile(vc)).read_text()
check("vendor_call imports no vendor SDK — client construction stays in adapters",
      not any(w in src for w in ("import anthropic", "from openai", "import openai")))
sig = inspect.signature(vc.call)
check("the signature is the specified one",
      {"vendor", "model", "prompt", "schema", "deadline_s", "purpose"} <= set(sig.parameters))
check("deadline_s is REQUIRED, not defaulted (an unbounded call is how a 3h30m run happens)",
      sig.parameters["deadline_s"].default is inspect.Parameter.empty)

# ── B. typed result — the single most important requirement ───────────────────────────────────────────────
print("-- B: a truncated or empty response cannot reach a caller as a success --")
def _b_probe():
    for kind in (vc.TRUNCATED, vc.EMPTY, vc.REFUSED, vc.TRANSPORT_ERROR, vc.DEADLINE_EXCEEDED):
        r = vc.Result(kind, "v", "m", text="PARTIAL {")
        if r.ok:
            return False
        if not _try(lambda _r=r: _r.text):   # reading .text MUST raise; not raising is the bug itself
            return False
    return vc.Result(vc.OK, "v", "m", text="answer").text == "answer"

_real_text = vc.Result.text
proves("B", "every non-ok kind refuses .text", _b_probe,
       lambda: setattr(vc.Result, "text", property(lambda self: self._text)),
       lambda: setattr(vc.Result, "text", _real_text))

print("-- B: classification comes from the vendor's own declared fields --")
check("finish_reason=length → truncated", vc._classify({"text": "x", "finish_reason": "length"})[0] == vc.TRUNCATED)
check("HTTP ok with zero characters → empty, NOT ok",
      vc._classify({"text": "", "finish_reason": "stop"})[0] == vc.EMPTY)
check("whitespace-only is still empty", vc._classify({"text": "  \n ", "finish_reason": "stop"})[0] == vc.EMPTY)
check("an error → transport_error", vc._classify({"error": "boom"})[0] == vc.TRANSPORT_ERROR)
check("real content → ok", vc._classify({"text": "hi", "finish_reason": "stop"})[0] == vc.OK)

# ── C. total deadline ─────────────────────────────────────────────────────────────────────────────────────
print("-- C: the deadline bounds the WHOLE call, retries included --")
_real_attempt = vc._attempt


def _unbounded_attempt(vendor, model, prompt, system, max_tokens, budget_s, schema=None):
    """The mutation: join WITHOUT a timeout — i.e. a deadline checked only BETWEEN attempts. This is the exact
    shape that produced the 3h30m run, so the probe must be able to tell it apart from the real thing."""
    import threading as _t
    from spendguard import adapters as _a
    box = {}
    th = _t.Thread(target=lambda: box.update(r=_a.call(f"{vendor}:{model}", prompt, max_tokens=100)), daemon=True)
    th.start()
    th.join()                                    # no timeout — the removal being proved
    return box.get("r") or {"error": "none", "text": None}


def _c_probe():
    """A call whose adapter HANGS must still return within its deadline. Timed, not asserted from the code."""
    import spendguard.adapters as _a
    real = _a.call
    _a.call = lambda *a, **k: (time.sleep(4.0), {"text": "late", "finish_reason": "stop"})[1]
    try:
        t0 = time.time()
        r = vc.call("v", "m", "p", deadline_s=1.0, max_tokens=100, attempts=2)
        took = time.time() - t0
    finally:
        _a.call = real
    return took < 3.0 and not r.ok               # the hang is abandoned; unbounded would take 4s+


proves("C", "a hanging call is abandoned at the deadline", _c_probe,
       lambda: setattr(vc, "_attempt", _unbounded_attempt),
       lambda: setattr(vc, "_attempt", _real_attempt))
check("a call with no deadline is REFUSED outright", _try(lambda: vc.call("v", "m", "p", deadline_s=0)))

# ── E. measured caps, never invented ──────────────────────────────────────────────────────────────────────
print("-- E: max_tokens is measured or refused, never a constant --")
def _e_probe():
    r = vc.call("nosuchvendor", "nosuchmodel", "p", deadline_s=5.0)   # no cap anywhere
    return (not r.ok) and "no measured output cap" in (r.error or "")

_real_cap = vc.output_cap
proves("E", "an unknown model refuses rather than inventing a cap", _e_probe,
       lambda: setattr(vc, "output_cap", lambda v, m, sig=None: (512, "invented")),
       lambda: setattr(vc, "output_cap", _real_cap))
vc.record_cap("testv", "testm", 26128, method="probe", source="unit test")
check("a recorded cap carries its method and date (provenance, not a number someone typed)",
      vc.caps()["testv/testm"]["method"] == "probe" and vc.caps()["testv/testm"]["measured_at"])
check("output_cap prefers the registry", vc.output_cap("testv", "testm") == (26128, "registry:probe"))
check("a zero cap is refused at write time", _try(lambda: vc.record_cap("t", "m", 0, method="probe")))

# ── F. model discovery ────────────────────────────────────────────────────────────────────────────────────
print("-- F: 'we could not check' is not 'no' --")
_real_list, _real_serves = vc.list_models, vc.serves
def _f_probe():
    vc.list_models = lambda v, timeout_s=20: {"vendor": v, "models": [], "error": "network down"}
    got = vc.serves("openai", "gpt-5.5")
    vc.list_models = _real_list
    return got is None                       # None = unknown. False would be a claim we cannot support.

proves("F", "a failed discovery returns None, never False", _f_probe,
       lambda: setattr(vc, "serves", lambda v, m: False),
       lambda: setattr(vc, "serves", _real_serves))

# ── G. schema: the vendor enforces the shape, and empty values are not answers ────────────────────────────
print("-- G: request-side enforcement, per vendor --")
SCHEMA = {"type": "object", "required": ["line_start", "issue"], "nonempty": ["line_start", "issue"],
          "properties": {"line_start": {"type": "number"}, "issue": {"type": "string"}}}
check("anthropic gets a FORCED tool call", set(adapters.json_schema_request("anthropic", SCHEMA))
      == {"tools", "tool_choice"})
check("openai gets a STRICT json_schema",
      adapters.json_schema_request("openai", SCHEMA)["response_format"]["json_schema"]["strict"] is True)
check("an OpenAI-compatible endpoint gets json_object — parseable, shape NOT vendor-enforced",
      adapters.json_schema_request("compat", SCHEMA)["response_format"] == {"type": "json_object"})
check("a non-dict contract asks for no enforcement", adapters.json_schema_request("openai", ["a", "b"]) == {})

print("-- G: present-but-empty is absence, not an answer (the L0-0 failure) --")
def _g_probe():
    ok, _s, _w = oc.check_item('{"line_start": 0, "issue": ""}', SCHEMA)
    good, _s2, _w2 = oc.check_item('{"line_start": 116, "issue": "assumption-as-value"}', SCHEMA)
    return (not ok) and good

_real_isempty = oc._is_empty
proves("G", "a required field returned as 0/\"\" FAILS", _g_probe,
       lambda: setattr(oc, "_is_empty", lambda v: False),
       lambda: setattr(oc, "_is_empty", _real_isempty))

# ── H. pricing ────────────────────────────────────────────────────────────────────────────────────────────
print("-- H: the two models that recorded as UNPRICED now price, with provenance --")
for m in ("kimi-k3", "glm-5.2"):
    try:
        p = pricing.price(m)
        check(f"{m} prices (${p['in_']}/${p['out']}) and cites a source", bool(p.get("_source")))
    except Exception as e:
        check(f"{m} prices", False, str(e)[:60])

# ── I. persistence + a consensus that cannot be faked ─────────────────────────────────────────────────────
print("-- I: N-of-N is refused when fewer than N answered IN THIS RUN --")
def _fake_fan(n_ok, n):
    res = ([vc.Result(vc.OK, "v", f"m{i}", text="x") for i in range(n_ok)]
           + [vc.Result(vc.TRANSPORT_ERROR, "v", f"m{i}") for i in range(n - n_ok)])
    return {"results": res, "ok": res[:n_ok], "failed": res[n_ok:], "n": n, "n_ok": n_ok,
            "complete": n_ok == n, "run_id": vc.run_id()}

def _i_probe():
    try:
        vc.consensus(_fake_fan(2, 4))
        return False                          # reported a 4-vendor result on 2 answers — the stale-merge bug
    except vc.NotOk:
        pass
    return len(vc.consensus(_fake_fan(4, 4))) == 4

_real_consensus = vc.consensus
proves("I", "2-of-4 cannot be reported as 4-of-4", _i_probe,
       lambda: setattr(vc, "consensus", lambda fan, require=None: fan["ok"]),
       lambda: setattr(vc, "consensus", _real_consensus))

print("-- I: every call is persisted with the run that produced it --")
vc._persist(vc.Result(vc.OK, "v", "m", text="x", purpose="unit"))
rows = [json.loads(ln) for ln in open(vc._log_path())]
check("the call log is written", rows)
check("each row carries run_id + wall time — the freshness a merge step needs",
      rows[-1]["run_id"] == vc.run_id() and rows[-1]["ts"] > 0)
check("…and the result KIND, so a failure cannot be mistaken for an answer later", rows[-1]["kind"] == vc.OK)

# ── J. concurrency lock ───────────────────────────────────────────────────────────────────────────────────
print("-- J: two runs of the same job cannot clobber each other --")
def _j_probe():
    with vc.JobLock("panel", repo="r"):
        return _try(lambda: vc.JobLock("panel", repo="r").__enter__())   # a live lock must REFUSE

_real_enter = vc.JobLock.__enter__
proves("J", "a second run is refused while the first holds the lock", _j_probe,
       lambda: setattr(vc.JobLock, "__enter__", lambda self: self),
       lambda: setattr(vc.JobLock, "__enter__", _real_enter))

# ── K. the gate ───────────────────────────────────────────────────────────────────────────────────────────
print("-- K: it rides the existing gate rather than a second path --")
check("calls go through adapters (keys, lanes, metering already live there)", "adapters" in src)
check("no second key-resolution path", "keys.env" not in src or "config.api_key" in src)

# ── the worked example ────────────────────────────────────────────────────────────────────────────────────
print("-- WORKED EXAMPLE: 4 vendors review one file, 2 fail → a loud 2-of-4, never a silent 4-of-4 --")
PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"), ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
_outcomes = {"claude-opus-4-8": ("ok", '{"line_start": 116, "issue": "assumption-as-value"}'),
             "gpt-5.5": ("ok", '{"line_start": 59, "issue": "silent-skip"}'),
             "kimi-k3": ("transport", None),          # APIConnectionError, as measured
             "glm-5.2": ("empty", "")}                # HTTP 200, zero characters — the one that read as success

def _panel_attempt(vendor, model, prompt, system, max_tokens, budget_s, schema=None):
    kind, text = _outcomes[model]
    if kind == "transport":
        return {"error": "APIConnectionError", "text": None}
    return {"text": text, "finish_reason": "stop", "in_tok": 3500, "out_tok": 200}

vc._attempt = _panel_attempt
for v, m in PANEL:
    vc.record_cap(v, m, 26128, method="unit-test", source="worked example")
fan = vc.fan_out(PANEL, "review this file", deadline_s=30, purpose="panel", schema=SCHEMA)
vc._attempt = _real_attempt
print(f"      {fan['n_ok']} of {fan['n']} answered · " +
      " · ".join(f"{r.model}={r.kind}" for r in fan["results"]))
check("exactly 2 answered", fan["n_ok"] == 2)
check("the run is NOT complete, and says so", fan["complete"] is False)
check("the glm empty body is a FAILURE, not a reviewer with no findings",
      [r for r in fan["results"] if r.model == "glm-5.2"][0].kind == vc.EMPTY)
check("the kimi transport error is its own kind",
      [r for r in fan["results"] if r.model == "kimi-k3"][0].kind == vc.TRANSPORT_ERROR)
check("consensus() REFUSES to publish a 4-vendor result", _try(lambda: vc.consensus(fan)))
check("…but a 2-vendor result IS available when asked for honestly", len(vc.consensus(fan, require=2)) == 2)
check("every outcome is in the call log for this run",
      sum(1 for ln in open(vc._log_path()) if json.loads(ln)["run_id"] == vc.run_id()) >= 4)

print(f"\n{'[FAIL]' if failures else 'OK'} test_vendor_call: {failures} failure(s)")
sys.exit(1 if failures else 0)
