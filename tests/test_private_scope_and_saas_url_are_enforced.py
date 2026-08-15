"""Two security/privacy guards a 4-LLM review of this repo found broken, pinned so they can't regress:

  saas._require_safe_url  — the Bearer ORG KEY rides every SaaS request, so the destination must be trusted. The
     old check used base.startswith("http://localhost"), which ALSO matches http://localhost.evil.com (and the
     127.0.0.1 prefix matches http://127.0.0.1.attacker.com) — a planted repo-local .spendguard.json url could
     exfiltrate the key in cleartext. The host must be parsed and matched exactly.

  share._shareable        — an insight the user marked scope='private' must NEVER be pushed to the shared server.
     The scope skip had been disabled with `... and False` ("scope is advisory; the scrubber is the real gate"),
     so private insights were exported. On a privacy contract, least-surprise wins: private is a HARD gate.

Offline, isolated home.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-sec-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import saas, share   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── saas: the Bearer org key only goes to https or a REAL loopback host ──────────────────────────────────────
ATTACKER = ["http://localhost.evil.com", "http://127.0.0.1.attacker.com/v1", "http://evil.com",
            "http://localhostx", "http://127.0.0.1x", "ftp://localhost", "http://10.0.0.5"]
for bad in ATTACKER:
    raised = False
    try:
        saas._require_safe_url(bad)
    except RuntimeError:
        raised = True
    ck(f"REJECTED cleartext/attacker url: {bad}", raised)

SAFE = ["https://api.example.com", "https://localhost", "http://localhost", "http://localhost:8000",
        "http://127.0.0.1:9", "http://[::1]:8080"]
for good in SAFE:
    raised = False
    try:
        saas._require_safe_url(good)
    except RuntimeError:
        raised = True
    ck(f"ALLOWED safe url: {good}", not raised)


# ── share: a private-scoped insight is never shared (scope is a HARD gate, not advisory) ──────────────────────
_INS = [
    {"scope": "private", "confidence": 0.9, "status": "active", "task_class": "PRIV"},
    {"scope": "team", "confidence": 0.9, "status": "active", "task_class": "TEAM"},
    {"scope": None, "confidence": 0.9, "status": "active", "task_class": "NONE"},
]
share.learn.insights_full = lambda: [dict(x) for x in _INS]
share.scrub = lambda ins: {"task_class": ins.get("task_class")}      # isolate the SCOPE gate from scrub internals
shared = share._shareable(min_conf=0.6, require_active=True)
tcs = {o.get("task_class") for o in shared}
ck("a PRIVATE-scoped insight is NOT shared", "PRIV" not in tcs, str(tcs))
ck("non-private insights ARE shared (team + unset)", {"TEAM", "NONE"} <= tcs, str(tcs))

print(f"\n{'[FAIL]' if fails else 'OK'} test_private_scope_and_saas_url_are_enforced: {fails} failure(s)")
sys.exit(1 if fails else 0)
