"""The event message envelope (emit.envelope) — every emitted event is a versioned, typed, id'd, timestamped
message { v, type, id, ts, ...payload }. Pure + tolerant: fills missing envelope fields, preserves the payload,
never raises. Offline, isolated home. Script-style."""
import os, sys, tempfile, re

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-emit-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import emit

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

e = emit.envelope({"kind": "batch", "provider": "openai", "cost": 1.5})
ck("envelope stamps the version", e["v"] == emit.EVENT_V)
ck("type defaults from the event's kind (no emitter change needed)", e["type"] == "batch")
ck("id is a unique hex id", isinstance(e["id"], str) and re.fullmatch(r"[0-9a-f]{32}", e["id"]))
ck("ts is a UTC ISO timestamp", isinstance(e["ts"], str) and e["ts"][:4].isdigit() and "T" in e["ts"])
ck("payload preserved verbatim", e["provider"] == "openai" and e["cost"] == 1.5 and e["kind"] == "batch")

ck("empty event → type 'event', still fully enveloped", (lambda x: x["v"] == emit.EVENT_V and x["type"] == "event" and "id" in x and "ts" in x)(emit.envelope({})))
ck("explicit type is respected (not overwritten by kind)", emit.envelope({"type": "savings", "kind": "batch"})["type"] == "savings")
ck("unique ids across calls", emit.envelope({})["id"] != emit.envelope({})["id"])

# tolerant + never raises (observability must not break enforcement)
ck("None → safe empty envelope, no raise", (lambda x: x["v"] == emit.EVENT_V and x["type"] == "event")(emit.envelope(None)))
unknown = emit.envelope({"future_field": {"x": 1}, "kind": "realtime"})
ck("unknown future fields preserved (tolerant reader)", unknown["future_field"] == {"x": 1} and unknown["type"] == "realtime")

# emit() itself never raises even with a throwing callback
emit.on_event(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
try:
    emit.emit({"kind": "batch", "cost": 0.1}); raised = False
except Exception:
    raised = True
ck("emit() swallows a callback error (never breaks the gate)", raised is False)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"emit_envelope: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
