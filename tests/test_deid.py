"""Client-side de-id (deid.py) — the deterministic floor that enforces spendguard's privacy contract at the
wire. Guards: (1) the floor masks every high-confidence identifier class; (2) generalizable signal (ratios,
model names) SURVIVES; (3) Presidio-absent degrades to the floor and never raises; (4) engine=off / entities /
config+env knobs behave; (5) the WIRING — share._scrub_text AND the work-done egress builder actually route
through deid (so a future edit that bypasses it fails here). Offline, no network, zero spend."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-deid-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ambient overrides must not leak into the config-driven assertions below (the pytest runner sets
# SPENDGUARD_TEST_ISOLATED itself, so this path — not the re-exec above — is what runs under CI)
os.environ.pop("SPENDGUARD_DEID_ENGINE", None)
os.environ.pop("SPENDGUARD_DEID_ENTITIES", None)

from spendguard import deid

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

R = lambda s, **k: deid.redact(s, engine="regex", **k)

# ── the deterministic floor masks each identifier class ──
ck("email", R("ping bob.smith@acme.co about it") == "ping <EMAIL> about it")
ck("US phone", "<PHONE>" in R("call 415-555-0132 today") and "555" not in R("call 415-555-0132 today"))
ck("phone (parens)", "<PHONE>" in R("(415) 555-0132"))
ck("SSN", R("ssn 123-45-6789") == "ssn <SSN>")
ck("OpenAI key", "<API_KEY>" in R("key sk-abcdEFGH1234abcdEFGH1234xyz here"))
ck("Anthropic key", "<API_KEY>" in R("sk-ant-abcdEFGH1234abcdEFGH1234xyz"))
ck("AWS key", "<API_KEY>" in R("AKIAIOSFODNN7EXAMPLE"))
ck("GitHub token", "<API_KEY>" in R("ghp_" + "a" * 36))
ck("JWT", "<API_KEY>" in R("eyJhbGciOiJIUzI1NiIsIn.eyJzdWIiOiIxMjM0NTY.SflKxwRJSMeKKF2QT4"))
ck("Bearer", "<API_KEY>" in R("Authorization: Bearer abcDEF123456ghiJKL789"))
ck("PEM private key", R("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----") == "<PRIVATE_KEY>")
ck("IPv4", R("from 192.168.10.254 now") == "from <IP> now")
ck("IPv6", "<IP>" in R("at 2001:0db8:85a3:0000:0000:8a2e:0370:7334 ok"))
ck("USD amount", "<EMAIL>" not in R("cost $1,127 total") and "$1,127" not in R("cost $1,127 total"))
ck("bare price pair", "$X/$Y" in R("priced 2.50/15.00 per 1M"))

# ── credit cards: Luhn-valid masked, invalid left alone (no masking random 16-digit IDs) ──
ck("valid card masked", "<CREDIT_CARD>" in R("card 4111 1111 1111 1111"))
ck("invalid 16-digit NOT masked", "<CREDIT_CARD>" not in R("order 4111 1111 1111 1112"))

# ── generalizable signal SURVIVES (the whole point: keep the rule, drop the identity) ──
ck("ratio kept", "26x" in R("a 26x speedup with 10x fewer calls"))
ck("model name kept", "claude-opus-4-8" in R("use claude-opus-4-8 and gpt-5.5") and "gpt-5.5" in R("use claude-opus-4-8 and gpt-5.5"))
ck("timestamp 12:34 not masked as IP", "<IP>" not in R("at 12:34 pm"))

# ── drop: the caller's private intent label → <task> (honored even when engine=off) ──
ck("drop intent", "<task>" in R("work on SecretClient onboarding", drop=["SecretClient"]) and "SecretClient" not in R("work on SecretClient onboarding", drop=["SecretClient"]))
off = deid.redact("ship SecretClient to bob@x.com", engine="off", drop=["SecretClient"])
ck("engine=off: PII passes, drop still applies", "bob@x.com" in off and "<task>" in off and "SecretClient" not in off)

# ── entities filter restricts the floor ──
only_email = deid.redact("bob@x.com 415-555-0132", engine="regex", entities=["EMAIL"])
ck("entities=EMAIL masks email only", "<EMAIL>" in only_email and "<PHONE>" not in only_email)

# ── never raises / passthrough ──
ck("None passthrough", deid.redact(None) is None)
ck("non-str passthrough", deid.redact(12345) == 12345)
ck("empty passthrough", deid.redact("   ") == "   ")

# ── Presidio engine: masks + never raises, whether or not Presidio is installed ──
deid._PRESIDIO = None  # reset the memoized probe
got = deid.redact("email bob@x.com", engine="presidio")
ck("presidio engine masks + never raises (floor if absent, NER if present)", "<EMAIL>" in got)
ck("presidio probe resolves to a definite state (False=absent, tuple=ready)",
   deid._PRESIDIO is False or isinstance(deid._PRESIDIO, tuple))

# ── config-driven engine + env override ──
from spendguard import config
def write_cfg(d):
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.CONFIG_JSON.write_text(json.dumps(d))
    config._cfg._cache = None
write_cfg({"deid": {"engine": "off"}})
ck("config deid.engine=off honored (no engine arg)", deid.redact("bob@x.com") == "bob@x.com")
os.environ["SPENDGUARD_DEID_ENGINE"] = "regex"
ck("env SPENDGUARD_DEID_ENGINE overrides config", deid.redact("bob@x.com") == "<EMAIL>")
del os.environ["SPENDGUARD_DEID_ENGINE"]

# ── WIRING guard 1: share._scrub_text routes through deid ──
from spendguard import share
write_cfg({})  # default engine=regex
ck("share._scrub_text masks email (routes through deid)", "<EMAIL>" in share._scrub_text("mail bob@x.com re $500"))
ck("share._scrub_text still strips $ + intent", "$500" not in share._scrub_text("acme spent $500", intent="acme"))

# ── WIRING guard 2: the work-done egress builder de-ids commits + summary ──
from spendguard import saas, workdone
saas_conn, saas_cok, saas_flt = saas.conn, saas.contributor_ok, saas._project_filter
wd_roll, wd_sum = workdone.rollup, workdone.load_summaries
try:
    saas.conn = lambda: {"visibility": "team"}
    saas.contributor_ok = lambda: (True, "")
    saas._project_filter = lambda c: None
    workdone.load_summaries = lambda: {"proj": "Followed up with jane@corp.com on the rollout"}
    workdone.rollup = lambda since=None, by="month": [{
        "period": "2026-06", "project": "proj", "active_days": 1, "n_commits": 1, "n_batch_calls": 0,
        "commits": ["Fix login for customer john@acme.com"], "intents": {},
    }]
    out = saas.push_workdone(dry=True)
    w = (out.get("work") or [{}])[0]
    ck("work-done commit subject de-id'd", "<EMAIL>" in (w.get("commits") or [""])[0] and "john@acme.com" not in (w.get("commits") or [""])[0])
    ck("work-done summary de-id'd", "<EMAIL>" in w.get("summary", "") and "jane@corp.com" not in w.get("summary", ""))
finally:
    saas.conn, saas.contributor_ok, saas._project_filter = saas_conn, saas_cok, saas_flt
    workdone.rollup, workdone.load_summaries = wd_roll, wd_sum

print(("[OK]" if not fails else "[FAIL]") + " deid: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
