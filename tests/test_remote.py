"""remote.py — enforce the gate on distributed/remote compute. Script-style, offline (mock SSH runner, no live box).

Guards: onstart snippet installs+hooks+verifies; verify is FAIL-CLOSED (error/uncertainty → not-ok); sync pulls the
box's realtime log and rolls it into the local ledger scoped to the project, IDEMPOTENTLY (re-sync replaces, never
double-counts — the API-spend protocol's no-double-count rule for remote spend)."""
import os, sys, tempfile, datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-remote-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import remote, budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

TODAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


class _R:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout; self.stderr = stderr


# ── onstart: provision-time gate snippet ─────────────────────────────────────
snip = remote.onstart_snippet()
ck("onstart: installs the package", "pip install -q llm-spendguard" in snip)
ck("onstart: writes the gate hook", "install-hook --user" in snip)
ck("onstart: verifies ENFORCING", "ENFORCING HERE" in snip and "doctor" in snip)
ck("onstart: secret-free (no api key on the box)", "api_key" not in snip.lower() and "sg_" not in snip)
ck("onstart --from-git: uses git source", "git+https://github.com/llmspendguard" in remote.onstart_snippet(from_git=True))

# ── verify: FAIL-CLOSED ──────────────────────────────────────────────────────
ok, _ = remote.verify("ssh box", _run=lambda *a, **k: _R(stdout="spend gate: ENABLED\n  ENFORCING HERE: 🟢 YES — gated\n"))
ck("verify: ENFORCING → ok", ok is True)
ok, _ = remote.verify("ssh box", _run=lambda *a, **k: _R(stdout="  ENFORCING HERE: 🔴 NO — not gated\n"))
ck("verify: NOT enforcing → not ok", ok is False)
ok, d = remote.verify("ssh box", _run=lambda *a, **k: _R(stdout="bash: spendguard: command not found\n"))
ck("verify: no gate at all → not ok", ok is False)
ok, d = remote.verify("ssh box", _run=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh timeout")))
ck("verify: SSH error → FAIL-CLOSED (not ok)", ok is False and "fail-closed" in d)

# ── budget.ingest_remote: idempotent + project-scoped ────────────────────────
rows = [{"day": TODAY, "model": "claude-haiku-4-5", "provider": "anthropic", "cost": 2.50},
        {"day": TODAY, "model": "claude-haiku-4-5", "provider": "anthropic", "cost": 1.50}]
n, usd = budget.ingest_remote("m2a-h200", "manga2anime", rows)
ck("ingest_remote: records both rows", n == 2 and abs(usd - 4.0) < 1e-9)
ck("ingest_remote: scoped to the project (actual-$)", abs(budget.spent_since(TODAY, project="manga2anime") - 4.0) < 1e-9)
ck("ingest_remote: does NOT leak into another project", budget.spent_since(TODAY, project="other") == 0.0)
# re-ingest the SAME box → replaces, never doubles
budget.ingest_remote("m2a-h200", "manga2anime", rows)
ck("ingest_remote: IDEMPOTENT (re-sync replaces, no double-count)", abs(budget.spent_since(TODAY, project="manga2anime") - 4.0) < 1e-9)
# a different box adds independently
budget.ingest_remote("m2a-3090", "manga2anime", [{"day": TODAY, "model": "x", "provider": "anthropic", "cost": 1.0}])
ck("ingest_remote: different box accumulates", abs(budget.spent_since(TODAY, project="manga2anime") - 5.0) < 1e-9)

# ── sync: pull the box's realtime log → ingest (mock the SSH cat) ────────────
log = ('{"day":"%s","model":"claude-haiku-4-5","provider":"anthropic","cost":3.0}\n'
       '{"day":"%s","model":"claude-haiku-4-5","provider":"anthropic","cost":0}\n' % (TODAY, TODAY))
res = remote.sync("ssh -p 22 root@box", project="manga2anime", label="m2a-h200",
                  _run=lambda *a, **k: _R(stdout=log))
ck("sync: pulled the non-zero row", res["rows"] == 1 and abs(res["usd"] - 3.0) < 1e-9)
ck("sync: re-ingested under the box label (replaced the earlier 4.0)",
   abs(budget.spent_since(TODAY, project="manga2anime") - (3.0 + 1.0)) < 1e-9)  # m2a-h200 now 3.0, m2a-3090 still 1.0

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
