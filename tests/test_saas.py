"""Offline test for the SaaS client seam + config resolution + coverage probe. No network:
all paths exercised are the not-connected / graceful ones."""
import os, sys, json, tempfile, io
from contextlib import redirect_stdout

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, saas

# Isolate the CWD too, not just SPENDGUARD_HOME: saas_config overlays the nearest `.spendguard.json` found walking up
# from cwd, so running the suite from a repo that HAS one (e.g. llm-spendguard's own org config) would leak into these
# assertions. Chdir into the temp home (no .spendguard.json above it) so config resolution is hermetic.
os.chdir(str(config.HOME))

def check(label, ok):
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    assert ok, label

print("-- saas_config: file + env overlay, enabled coercion --")
home = config.HOME
(home / "saas.json").write_text(json.dumps({"enabled": False, "url": "https://llmspendguard.com", "visibility": "private"}))
config._cfg._cache = None
c = config.saas_config()
check("reads url from saas.json", c["url"] == "https://llmspendguard.com")
check("enabled coerced to bool False", c["enabled"] is False)
check("visibility defaults sanely", c["visibility"] == "private")
os.environ["SPENDGUARD_SAAS"] = "1"; os.environ["SPENDGUARD_SAAS_KEY"] = "tok_test"
c2 = config.saas_config()
check("env enables", c2["enabled"] is True)
check("env supplies secret key (not from repo)", c2["api_key"] == "tok_test")

print("-- ready(): needs enabled + url + key --")
ok, _ = saas.ready()
check("ready() true when enabled+url+key all set", ok)
os.environ.pop("SPENDGUARD_SAAS_KEY")
ok2, reason = saas.ready()
check("ready() false without key", (not ok2) and "api_key" in reason)

print("-- _request fails CLOSED-ish (clear error), never silently 'succeeds' offline --")
os.environ.pop("SPENDGUARD_SAAS", None)
raised = False
try:
    saas.ping()                                  # not enabled now → must raise a clear RuntimeError, not hang/return junk
except RuntimeError as e:
    raised = "not connected" in str(e)
check("ping() raises clear 'not connected' when off", raised)

print("-- visibility=private => push is a no-op (nothing leaves) --")
os.environ["SPENDGUARD_SAAS"] = "1"; os.environ["SPENDGUARD_SAAS_URL"] = "https://x"; os.environ["SPENDGUARD_SAAS_KEY"] = "k"
r = saas.push_rollup()
check("private push skipped (no network attempted)", isinstance(r, dict) and "skipped" in r)

print("-- sync cadence: interval drives due(); sync(if_due) is cron-safe --")
os.environ["SPENDGUARD_SYNC_INTERVAL"] = "daily"
d, _why = saas.due()
check("daily + never-synced => due", d is True)
os.environ["SPENDGUARD_SYNC_INTERVAL"] = "off"
d2, why2 = saas.due()
check("interval=off => not due (manual only)", (d2 is False) and "off" in why2)
os.environ["SPENDGUARD_SYNC_INTERVAL"] = "daily"
# not connected (private visibility still 'connected' for ready(); make it not-ready by dropping key)
os.environ.pop("SPENDGUARD_SAAS_KEY")
rs = saas.sync(if_due=True)
check("sync(if_due) when not connected => skipped, never raises", isinstance(rs, dict) and "skipped" in rs)
for k in ("SPENDGUARD_SAAS", "SPENDGUARD_SAAS_URL", "SPENDGUARD_SAAS_KEY", "SPENDGUARD_SYNC_INTERVAL"):
    os.environ.pop(k, None)
check("client config holds NO team_id/org_id (server resolves hierarchy from the key)",
      "team_id" not in config.saas_config() and "org_id" not in config.saas_config())

print("-- status() and saas.cmd() don't crash --")
buf = io.StringIO()
with redirect_stdout(buf):
    saas.status()
    saas.cmd(["status"])
check("status output mentions the client seam", "client seam" in buf.getvalue())

print("-- coverage probe runs (bounded, no recursive HOME walk) and returns rows --")
from spendguard import setup
ver, has, enf = setup._probe(sys.executable)            # this interpreter: importable? gated?
check("_probe returns this python's version", ver is not None and ver.count(".") >= 1)
buf2 = io.StringIO()
with redirect_stdout(buf2):
    rc = setup.coverage([])                              # must not hang; rc 0 (all gated) or 2 (a gap)
check("coverage() prints the per-interpreter table", "PER-INTERPRETER" in buf2.getvalue())
check("coverage() returns 0 or 2", rc in (0, 2))

print("done.")
