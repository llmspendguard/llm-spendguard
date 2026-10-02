"""A sweep-sourced metered 'unreachable' row for a provider that can NEVER be chat-probed is STALE RESIDUE, and
health_reds drops it (and deletes it) instead of alarming the receipt forever.

Regression for caller-feedback #9: `⚠ spendguard: 1 resource(s) unreachable (voyage)` fired on ~every hook all
session. voyage is embed-ONLY, so _metered_target returns None (no CHAT probe target) and the sweep correctly drops
it — but its last chat-probe 'unreachable' row kept alarming because nothing cleared it. The fix: health_reds treats
a sweep-metered red for a provider with NO derivable probe target as 'unknown', not down, and deletes the residue so
it never fires again. The signal is PROVIDER-INTRINSIC (embed-only is a property of the provider, not the key), so a
provider that is chat-probeable but merely keyless keeps its red; and any error resolving the target fails SAFE
(keep the red — never hide a real one).

Offline, isolated SPENDGUARD_HOME, zero spend.
"""
import os
import sys
import tempfile
import datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-healthresidue-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reliability  # noqa: E402


class Checks:
    """Accumulates failures on its OWN instance (self), so there is no module-level mutable counter."""
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()
TS = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def seed(resource, kind, source, reachable=0):
    db = reliability._health_db()
    db.execute("INSERT OR REPLACE INTO lane_health (resource,kind,reachable,reason,fix,command,ts,source,notified_ts) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (resource, kind, reachable, "down for the test", "a fix", "", TS, source, None))
    db.commit()


def clear():
    db = reliability._health_db()
    db.execute("DELETE FROM lane_health")
    db.commit()


def row_exists(resource):
    db = reliability._health_db()
    return db.execute("SELECT 1 FROM lane_health WHERE resource=?", (resource,)).fetchone() is not None


_orig_target = reliability._metered_target

# ── Scenario A: a provider with NO probe target (embed-only) → red removed + row DELETED; a chat-probeable one kept ─
clear()
seed("voyage", "metered", "sweep")     # embed-only → _metered_target None → STALE residue
seed("openai", "metered", "sweep")     # chat-probeable → a real red
seed("codex", "lane", "sweep")         # a lane red — the metered filter must not touch it
reliability._metered_target = lambda prov: None if prov == "voyage" else "gpt-x"
try:
    reds = reliability.health_reds()
    names = {r["resource"] for r in reds}
    ck("the unprobeable provider (voyage) is dropped from the reds", "voyage" not in names, extra=f"names={names}")
    ck("a chat-probeable provider (openai) keeps its red", "openai" in names, extra=f"names={names}")
    ck("a lane red is untouched by the metered filter (codex)", "codex" in names, extra=f"names={names}")
    ck("the stale voyage row is DELETED from the DB (self-clean)", not row_exists("voyage"))
    ck("the real openai row is NOT deleted", row_exists("openai"))
    alert = reliability.health_alert()
    ck("health_alert no longer names voyage", alert is not None and "voyage" not in alert, extra=repr(alert))
    ck("health_alert still names the real red (openai)", alert is not None and "openai" in alert, extra=repr(alert))
finally:
    reliability._metered_target = _orig_target

# ── Scenario B: an error resolving the target ⇒ keep the red (fail safe), and do not delete ───────────────────────
clear()
seed("keepme", "metered", "sweep")


def _boom(prov):
    raise RuntimeError("target resolution failed")


reliability._metered_target = _boom
try:
    reds = reliability.health_reds()
    ck("a target-resolution error KEEPS the red (fail safe)", "keepme" in {r["resource"] for r in reds})
    ck("a target-resolution error does NOT delete the row", row_exists("keepme"))
finally:
    reliability._metered_target = _orig_target

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_health_reds_drops_unprobed_metered_residue: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
