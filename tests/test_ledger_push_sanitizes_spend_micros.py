"""A corrupt day_total is surfaced and isolated before the strict server sees a ledger push."""
import contextlib
import io
import json
import math

from spendguard import saas


def check(name, condition):
    if not condition:
        raise AssertionError(name)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"accepted": 2}'


def server_numeric_rule_accepts(payload):
    """Mirror server src/lib/core.ts validateLedgerPayload lines 122-123."""
    fields = ("spend_micros", "calls", "in_tokens", "out_tokens", "cached_in_tokens")
    return all(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                 and math.isfinite(value) and value >= 0)
               for row in payload["day_totals"] for value in (row.get(field) for field in fields))


valid_a = {"day": "2026-10-14", "provider": "openai", "model": "good-a", "channel": "batch",
           "project": "alpha", "spend_micros": 11, "calls": 1, "in_tokens": 2}
bad_spend = {"day": "2026-10-14", "provider": "openai", "model": "bad-spend", "channel": "batch",
             "project": "billing", "spend_micros": -7, "calls": 1}
valid_b = {"day": "2026-10-15", "provider": "anthropic", "model": "good-b", "channel": "claude-code",
           "project": "beta", "billed": False, "spend_micros": 23, "calls": 2, "out_tokens": 4}
bad_calls = {"day": "2026-10-15", "provider": "anthropic", "model": "bad-calls", "channel": "batch",
             "project": "ops", "spend_micros": 8, "calls": -1}
bad_nan = {"day": "2026-10-16", "provider": "openai", "model": "bad-nan", "channel": "codex",
           "project": "tools", "billed": False, "spend_micros": float("nan"), "calls": 1}
dirty = {"visibility": "org", "day_totals": [valid_a, bad_spend, valid_b, bad_calls, bad_nan]}
check("pre-fix payload would be rejected by the server numeric oracle", not server_numeric_rule_accepts(dirty))

captured = []
original_ready = saas.ready
original_connection = saas.saas_connection
original_urlopen = saas.urllib.request.urlopen
original_ssl_context = saas.config.ssl_context
try:
    saas.ready = lambda: (True, "configured")
    saas.saas_connection = lambda: {"url": "https://example.test", "api_key": "test-key"}
    saas.config.ssl_context = lambda: None

    def capture(request, **_kwargs):
        captured.append(json.loads(request.data))
        return _Response()

    saas.urllib.request.urlopen = capture
    warnings = io.StringIO()
    with contextlib.redirect_stderr(warnings):
        result = saas._request("POST", "/v1/ledger", dirty)
finally:
    saas.ready = original_ready
    saas.saas_connection = original_connection
    saas.urllib.request.urlopen = original_urlopen
    saas.config.ssl_context = original_ssl_context

sent = captured[0]
check("good rows still pushed", result == {"accepted": 2} and sent["day_totals"] == [valid_a, valid_b])
check("captured payload satisfies server numeric oracle", server_numeric_rule_accepts(sent))
warning = warnings.getvalue()
for expected in ("bad-spend", "billing", "spend_micros=-7", "bad-calls", "calls=-1",
                 "bad-nan", "spend_micros=nan", "REAL billed spend"):
    check(f"warning names {expected}", expected in warning)

clean = {"visibility": "org", "day_totals": [valid_a, valid_b], "replace": [{"channel": "batch"}]}
check("clean payload object and numerics pass through unchanged",
      saas._sanitize_ledger_day_totals_for_server(clean) is clean
      and saas._sanitize_ledger_day_totals_for_server(clean) == clean)

print("OK: ledger push excludes and surfaces invalid numeric rows while preserving valid rows")
