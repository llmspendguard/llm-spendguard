"""Offline regression guards for resolved effort and whole executable request shape."""
import json
import os
import sqlite3
from unittest.mock import patch

os.environ["SPENDGUARD_CAPTURE_LIVE"] = "1"
from spendguard import adapters, bakeoff, callio  # noqa: E402

_CALLIO_FIDELITY_INTENT = "callio-effort-shape-guard"
_CALLIO_FIDELITY_MODEL = "openai:gpt-5.6-luna"
_CALLIO_FIDELITY_EFFORT = "high"
_CALLIO_FIDELITY_CAP = 64
_CALLIO_FIDELITY_SYSTEM = "Instructions with important final requirement. " * _CALLIO_FIDELITY_CAP
_CALLIO_FIDELITY_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "description": _CALLIO_FIDELITY_SYSTEM}},
                           "required": ["answer"], "additionalProperties": False}
_CALLIO_FIDELITY_CEILING = 4096
_CALLIO_FIDELITY_SHAPE_BATCH = "shape-batch"
_CALLIO_FIDELITY_SHAPE_ROW = "shape-row"
_CALLIO_FIDELITY_REFILL_BATCH = "refill-batch"
_CALLIO_FIDELITY_REFILL_ROW = "refill-row"
_CALLIO_FIDELITY_LEGACY_ID = "legacy"

with patch.object(adapters, "_call_guarded", return_value={
        "text": "answer", "model": _CALLIO_FIDELITY_MODEL, "provider": "openai", "cost": 0,
        "chosen_effort": _CALLIO_FIDELITY_EFFORT, "in_tok": 1, "out_tok": 1, "error": None}):
    adapters.call(_CALLIO_FIDELITY_MODEL, "a workload prompt", sig=_CALLIO_FIDELITY_INTENT,
                  reasoning="low", system=_CALLIO_FIDELITY_SYSTEM, schema=_CALLIO_FIDELITY_SCHEMA)
row = callio._callio_db().execute("SELECT effort FROM call_io WHERE intent=?", (_CALLIO_FIDELITY_INTENT,)).fetchone()
assert row == (_CALLIO_FIDELITY_EFFORT,), row

callio.record_io_sample(_CALLIO_FIDELITY_INTENT, "openai", _CALLIO_FIDELITY_MODEL, _CALLIO_FIDELITY_SHAPE_BATCH, _CALLIO_FIDELITY_SHAPE_ROW,
                        "short prompt", "short reply", system=_CALLIO_FIDELITY_SYSTEM,
                        req_schema=_CALLIO_FIDELITY_SCHEMA, req_max_tokens=_CALLIO_FIDELITY_CEILING,
                        cap_chars=_CALLIO_FIDELITY_CAP, effort=_CALLIO_FIDELITY_EFFORT)
shape_row = callio._callio_db().execute(
    "SELECT system,req_schema,req_max_tokens,truncated,shape_complete FROM call_io WHERE batch=?", (_CALLIO_FIDELITY_SHAPE_BATCH,)).fetchone()
assert shape_row == (_CALLIO_FIDELITY_SYSTEM, json.dumps(_CALLIO_FIDELITY_SCHEMA), _CALLIO_FIDELITY_CEILING, 0, 1), shape_row
callio.record_io_sample(_CALLIO_FIDELITY_INTENT, "openai", _CALLIO_FIDELITY_MODEL, _CALLIO_FIDELITY_REFILL_BATCH, _CALLIO_FIDELITY_REFILL_ROW, "p", "o")
callio.record_io_sample(_CALLIO_FIDELITY_INTENT, "openai", _CALLIO_FIDELITY_MODEL, _CALLIO_FIDELITY_REFILL_BATCH, _CALLIO_FIDELITY_REFILL_ROW, "p", "o",
                        system=_CALLIO_FIDELITY_SYSTEM, req_schema=_CALLIO_FIDELITY_SCHEMA, effort=_CALLIO_FIDELITY_EFFORT)
refill = callio._callio_db().execute("SELECT system,req_schema,effort FROM call_io WHERE batch=?", (_CALLIO_FIDELITY_REFILL_BATCH,)).fetchone()
assert refill == (_CALLIO_FIDELITY_SYSTEM, json.dumps(_CALLIO_FIDELITY_SCHEMA), _CALLIO_FIDELITY_EFFORT), refill
samples = bakeoff._sample_prompts(_CALLIO_FIDELITY_INTENT, _CALLIO_FIDELITY_CAP, include_shape=True)
assert any(s["schema"] == _CALLIO_FIDELITY_SCHEMA and s["effort"] == _CALLIO_FIDELITY_EFFORT for s in samples)
legacy = sqlite3.connect(":memory:")
callio._ensure_callio_schema(legacy)
legacy.execute("ALTER TABLE call_io DROP COLUMN shape_complete")
legacy.execute("INSERT INTO call_io(id,system,req_schema) VALUES (?,?,?)", (_CALLIO_FIDELITY_LEGACY_ID, _CALLIO_FIDELITY_SYSTEM, "partial schema"))
callio._ensure_callio_schema(legacy)
assert legacy.execute("SELECT shape_complete FROM call_io WHERE id=?", (_CALLIO_FIDELITY_LEGACY_ID,)).fetchone() == (0,)
print("OK callio effort and executable shape fidelity")
# An absent refill cannot erase a legacy incomplete contract or certify it complete.
callio._callio_db().execute("UPDATE call_io SET shape_complete=0 WHERE batch=?", (_CALLIO_FIDELITY_SHAPE_BATCH,))
callio._callio_db().commit()
callio.record_io_sample(_CALLIO_FIDELITY_INTENT, "openai", _CALLIO_FIDELITY_MODEL,
                        _CALLIO_FIDELITY_SHAPE_BATCH, _CALLIO_FIDELITY_SHAPE_ROW, "short prompt", "short reply")
assert callio._callio_db().execute(
    "SELECT system,req_schema,shape_complete FROM call_io WHERE batch=?", (_CALLIO_FIDELITY_SHAPE_BATCH,)
).fetchone() == (_CALLIO_FIDELITY_SYSTEM, json.dumps(_CALLIO_FIDELITY_SCHEMA), 0)
before = callio.count_rows(_CALLIO_FIDELITY_INTENT, _CALLIO_FIDELITY_MODEL)
for recorded_effort in (None, _CALLIO_FIDELITY_EFFORT):
    callio.capture_live(_CALLIO_FIDELITY_INTENT, "openai", _CALLIO_FIDELITY_MODEL,
                        "identical workload prompt", "reply", effort=recorded_effort,
                        system=_CALLIO_FIDELITY_SYSTEM, req_schema=_CALLIO_FIDELITY_SCHEMA)
assert callio.count_rows(_CALLIO_FIDELITY_INTENT, _CALLIO_FIDELITY_MODEL) == before + 2
