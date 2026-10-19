"""Shared exception injection and call observation for script-style tests."""

import asyncio
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial


def callable_raises_exception(fn, exc=Exception, *, unexpected_returns_false=False):
    """Observe the expected exception, preserving the caller's unrelated-error policy."""
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        if unexpected_returns_false:
            return False
        raise


def raise_test_exception(*args, exception_factory, **kwargs):
    """Raise a fresh configured exception, ignoring the injected call's arguments."""
    raise exception_factory()


async def async_return_value(*args, return_value, **kwargs):
    """Return the configured sentinel from an async injected call."""
    return return_value


def execute_gate_test_call(w, is_async, **kw):
    """Invoke a gate wrapper with its script-test receiver."""
    return asyncio.run(w(None, **kw)) if is_async else w(None, **kw)


def raise_gate_test_error(*args, **kwargs):
    """Inject the gate tests' controlled implementation failure."""
    raise_test_exception(exception_factory=partial(ValueError, GATE_TEST_ERROR_MESSAGE))


GATE_TEST_ERROR_MESSAGE = "injected gate bug"


class ScriptCheckAccumulator:
    """Keep script-check failures local and preserve caller-specific detail spacing."""

    def __init__(self, *, detail_prefix="  — "):
        self.fails = 0
        self.detail_prefix = detail_prefix

    @property
    def f(self):
        return self.fails

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{(self.detail_prefix + extra) if extra and not cond else ''}")


def request_test_json(port, method, path, body=None, headers=None, *, base_url, timeout):
    """Encode a JSON request and decode success or HTTP-error response bodies."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base_url.format(port=port) + path, data=data,
                                     method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def last_test_wire_effort(wire_calls, *, missing_call=None):
    """Read effort from the last captured request, retaining the missing-call policy."""
    return (wire_calls[-1] or {}).get("reasoning_effort") if wire_calls else missing_call


def collect_daemon_concurrent_turns(daemon_factory, prompts, *, timeout):
    """Collect ordered concurrent warm replies and always shut down their daemon."""
    daemon = daemon_factory()
    try:
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            futures = [pool.submit(daemon.run_warm, prompt) for prompt in prompts]
            return [future.result(timeout=timeout) for future in futures]
    finally:
        daemon.shutdown()


def collect_daemon_dead_pipe_waiters(daemon_factory, waiter_ids, *, process_timeout, event_timeout):
    """Terminate a daemon pipe and observe its registered waiters before shutdown."""
    daemon = daemon_factory()
    try:
        process = daemon.ensure_running()
        waiters = [daemon._register_waiter(waiter_id) for waiter_id in waiter_ids]
        process.terminate()
        process.wait(timeout=process_timeout)
        awakened = all(waiter["event"].wait(event_timeout) for waiter in waiters)
        dead = all(waiter["dead"] for waiter in waiters) if awakened else False
        return awakened, dead
    finally:
        daemon.shutdown()


def delayed_batch_results(items, *, event, timeout, response_for):
    """Wait for the configured release and produce one identified response per input."""
    event.wait(timeout=timeout)
    return {cid: response_for(prompt) for cid, prompt in items}


def seed_test_spend_event(day, provider, model, kind, cost, project, *,
                          timestamp_suffix, source, dedup_fields, conv=None, basis=None):
    """Record a dated fixture charge with the caller's exact attribution and identity."""
    from spendguard import budget

    charge_options = {}
    if conv is not None:
        charge_options["conv_id"] = conv
    if basis is not None:
        charge_options["basis"] = basis
    event = budget.charge_to_event(provider, model, kind, float(cost), **charge_options)
    event["project_primary"] = project
    event["projects"] = [project] if project else []
    event["occurred_at"] = event["ts_utc"] = day + timestamp_suffix
    event["source"] = event["recorded_by"] = source
    dedup_values = dict(day=day, provider=provider, model=model, kind=kind,
                        cost=cost, project=project, conv=conv)
    event["dedup_key"] = source + ":" + ":".join(str(dedup_values[field]) for field in dedup_fields)
    budget._ledger().record_event(event)


def scripted_process_results(results):
    """Yield supplied process results in order, repeating the last; expose call count."""
    sequence = list(results)
    calls = {"n": 0}

    def next_scripted_process_result(*args, **kwargs):
        result = sequence[calls["n"]] if calls["n"] < len(sequence) else sequence[-1]
        calls["n"] += 1
        return result

    return next_scripted_process_result, calls
