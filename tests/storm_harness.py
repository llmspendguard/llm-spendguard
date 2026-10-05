"""§0 EVIDENCE HARNESS for the 429-storm acceptance tests (offline, $0, un-fakeable by construction).

The panel's rule: "a test that can't be faked beats ten that can." This module is the substrate every storm test
stands on. It does NOT mock away the provider's rate limit — it SIMULATES a provider that will genuinely 429 a burst,
measures 429s at the CALLER boundary (never spendguard's own logs), and proves result integrity with per-request
canaries. It is imported by tests/test_incident_replay_storm.py (and future A–K tests).

Pieces (map to REQUIRED_TESTS_429_storm.md §0):
  FakeProvider   — E0.2: a rate-capped provider. Realtime calls exceeding `cap` per `window_s` get a REAL 429 (the
                   adapters 429 shape: status_code/http_status=429). A separate batch_submit() path has NO rate limit
                   (models the Batch API) and records that it was used. Counts realtime vs batch + 429s. Echoes each
                   request's canary so unbundling correctness is checkable. Installs by replacing adapters._call_once
                   — i.e. AFTER the real dispatch.admit/pacing runs, so it observes the governed egress rate.
  CallerCollector— E0.1: records every outcome the CALLER sees; surfaced_429 is counted HERE.
  canary/bijection — E0.4: a unique token per request; proves the right answer came back for the right request.

A control arm (drive FakeProvider WITHOUT the governor) MUST 429 (E0.3 "teeth"); the guarded arm (through spendguard)
must not. Real sliding-window on the wall clock + small caps keep it fast and deterministic enough that a burst
clearly exceeds the window while pacing clearly stays under it.
"""
import threading
import time

CANARY_PREFIX = "CANARY-"


def canary(i):
    """The unique marker embedded in request i; a correct response echoes it back."""
    return "%s%d" % (CANARY_PREFIX, i)


class FakeProvider:
    """A provider that genuinely throttles. `cap` realtime calls per `window_s` seconds; the (cap+1)th within the
    window gets a 429. batch_submit() is unthrottled (the Batch API). Thread-safe; counts everything."""

    def __init__(self, cap=20, window_s=2.0, latency_s=0.0):
        self.cap, self.window_s, self.latency_s = cap, window_s, latency_s
        self._lock = threading.Lock()
        self._realtime_ts = []                 # timestamps of admitted realtime calls, for the sliding window
        self.realtime_calls = 0                # realtime calls that were SERVED (not 429'd)
        self.realtime_429 = 0                  # realtime calls the provider REFUSED with a 429
        self.batch_calls = 0                   # requests served via the batch path
        self.batch_jobs = 0                    # number of batch jobs created
        self.served_canaries = []              # canaries the provider actually answered (realtime or batch)

    def _realtime(self, model, prompt):
        """One realtime call. Returns an adapters-shaped result dict; a 429 when the sliding-window cap is exceeded."""
        now = time.monotonic()
        with self._lock:
            self._realtime_ts = [t for t in self._realtime_ts if now - t < self.window_s]
            if len(self._realtime_ts) >= self.cap:
                self.realtime_429 += 1
                return {"provider": "fake", "model": model, "text": None, "in_tok": 0, "out_tok": 0, "latency": 0.0,
                        "cost": None, "finish_reason": None, "status_code": 429, "http_status": 429,
                        "error": "429 rate_limit_error (fake provider: >%d/%.0fs)" % (self.cap, self.window_s),
                        "retry_after": 1}
            self._realtime_ts.append(now)
            self.realtime_calls += 1
        if self.latency_s:
            time.sleep(self.latency_s)
        _can = _extract_canary(prompt)
        with self._lock:
            self.served_canaries.append(_can)
        # a real, billed-shaped success: echo the canary so the caller can prove correct demux.
        return {"provider": "fake", "model": model, "text": "ok %s" % _can, "in_tok": max(1, len(str(prompt)) // 4),
                "out_tok": 3, "latency": self.latency_s, "cost": 0.0001, "finish_reason": "stop", "status_code": 200}

    def batch_submit(self, items):
        """The Batch-API path: a list of (custom_id, model, prompt) → a dict custom_id->result. UNTHROTTLED. Records
        that the batch path was taken + how many jobs/items. This is what the auto-batch wiring must call on a storm."""
        with self._lock:
            self.batch_jobs += 1
        out = {}
        for cid, model, prompt in items:
            _can = _extract_canary(prompt)
            with self._lock:
                self.batch_calls += 1
                self.served_canaries.append(_can)
            out[cid] = {"provider": "fake", "model": model, "text": "ok %s" % _can, "in_tok": 1, "out_tok": 3,
                        "cost": 0.00005, "finish_reason": "stop", "status_code": 200}
        return out

    def install(self):
        """Replace adapters._call_once so EVERY governed realtime egress hits this provider (after admit/pacing)."""
        from spendguard import adapters
        self._orig = adapters._call_once

        def _fake_call_once(model, prompt, *a, **kw):
            return self._realtime(model, prompt)
        adapters._call_once = _fake_call_once
        return self

    def uninstall(self):
        from spendguard import adapters
        if getattr(self, "_orig", None):
            adapters._call_once = self._orig


def _extract_canary(prompt):
    s = str(prompt or "")
    i = s.find(CANARY_PREFIX)
    if i < 0:
        return None
    j = i + len(CANARY_PREFIX)
    k = j
    while k < len(s) and s[k].isdigit():
        k += 1
    return s[i:k]


class CallerCollector:
    """E0.1 — records every outcome the CALLER sees. surfaced_429 is measured HERE (not from spendguard logs)."""

    def __init__(self):
        self.results = []
        self.surfaced_429 = 0
        self.returned_canaries = []
        self._lock = threading.Lock()

    def record(self, r):
        with self._lock:
            self.results.append(r)
            if isinstance(r, dict):
                sc = r.get("status_code") or r.get("http_status")
                err = str(r.get("error") or "")
                if sc == 429 or "429" in err or "rate_limit" in err.lower():
                    self.surfaced_429 += 1
                self.returned_canaries.append(_extract_canary(r.get("text")))

    def bijection_ok(self, submitted_canaries):
        """E0.4 — the multiset of returned canaries equals the multiset submitted (none dropped/duplicated/mismatched)."""
        got = sorted(c for c in self.returned_canaries if c)
        want = sorted(submitted_canaries)
        return got == want
