"""SpendLedger foundation tests — proven BEFORE any consumer hooks up.

Step 1: schema + record/get — cost routes to the correct one of four columns, JSON round-trips, the deterministic id
dedups (no double-count), invalid events are rejected.
"""
import pytest
from spendguard.ledger import SpendLedger, COST_COLS


def _led(tmp_path):
    return SpendLedger(db_path=str(tmp_path / "ledger.db"))


def test_cost_routes_to_correct_column_and_json_roundtrips(tmp_path):
    led = _led(tmp_path)
    eid = led.record({"source": "reconstruction", "kind": "realtime", "usd": 220.0,
                      "provider": "openai", "model": "gpt-5.5", "model_kind": "completion",
                      "org": "Healiom", "team": "lmm", "projects": ["lmm", "medical-taxonomy"],
                      "cwd": "/Users/x/Documents/claude/lmm", "conv_id": "c1", "batch_id": "b1",
                      "cost_basis": "printed", "from_message_ids": ["m1", "m2"], "tags": ["realtime"],
                      "attr_what": "loinc stem pass", "attr_why": "cwd=lmm", "attr_how": "cwd-match"})
    ev = led.get(eid)
    assert ev["realtime_usd"] == 220.0
    assert ev["batch_usd"] == 0.0 and ev["est_chat_usd"] == 0.0 and ev["remote_usd"] == 0.0
    assert ev["projects"] == ["lmm", "medical-taxonomy"]
    assert ev["from_message_ids"] == ["m1", "m2"] and ev["tags"] == ["realtime"]
    assert ev["org"] == "Healiom" and ev["team"] == "lmm" and ev["attr_how"] == "cwd-match"


@pytest.mark.parametrize("kind,col", [("batch", "batch_usd"), ("realtime", "realtime_usd"),
                                      ("est_chat", "est_chat_usd"), ("remote", "remote_usd")])
def test_every_kind_routes_to_its_own_column(tmp_path, kind, col):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "s", "kind": kind, "usd": 5.0, "conv_id": kind}))
    assert ev[col] == 5.0
    assert sum(ev[c] for c in COST_COLS) == 5.0


def test_dedup_same_evidence_records_once(tmp_path):
    led = _led(tmp_path)
    base = {"source": "reconstruction", "kind": "realtime", "usd": 220.0,
            "provider": "openai", "model": "gpt-5.5", "conv_id": "c1", "batch_id": "b1",
            "attr_what": "loinc stem pass"}
    a = led.record(base)
    b = led.record({**base, "org": "Healiom", "team": "lmm"})
    assert a == b
    n = led._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    assert n == 1, f"double-count: {n} rows for the same evidence"


def test_different_work_is_distinct(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "reconstruction", "kind": "realtime", "usd": 1, "conv_id": "c1",
                    "attr_what": "loinc stem pass"})
    b = led.record({"source": "reconstruction", "kind": "realtime", "usd": 1, "conv_id": "c1",
                    "attr_what": "concept_bc adjudication"})
    assert a != b
    assert led._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0] == 2


def test_invalid_events_rejected(tmp_path):
    led = _led(tmp_path)
    with pytest.raises(ValueError):
        led.record({"source": "x", "provider": "openai"})
    with pytest.raises(ValueError):
        led.record({"source": "x", "kind": "weird", "usd": 1.0})
    with pytest.raises(ValueError):
        led.record({"kind": "realtime", "usd": 1.0})


# ── Step 2: query / rollup / by_repo ──

def test_rollup_total_billed_vs_estvalue_split(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "batch", "usd": 100.0, "conv_id": "a", "attr_what": "batch job"})
    led.record({"source": "x", "kind": "realtime", "usd": 5.0, "conv_id": "b", "attr_what": "rt run"})
    led.record({"source": "x", "kind": "remote", "usd": 20.0, "conv_id": "c", "attr_what": "gpu"})
    led.record({"source": "x", "kind": "est_chat", "usd": 50.0, "conv_id": "d", "attr_what": "claude code"})
    t = led.rollup()
    assert (t["batch_usd"], t["realtime_usd"], t["remote_usd"], t["est_chat_usd"]) == (100, 5, 20, 50)
    assert t["billed"] == 125      # batch+realtime+remote — NEVER est_chat
    assert t["est_value"] == 50    # est_chat is the separate axis
    assert t["n"] == 4


def test_rollup_by_org(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "realtime", "usd": 220.0, "org": "Healiom", "conv_id": "h", "attr_what": "loinc"})
    led.record({"source": "x", "kind": "realtime", "usd": 10.0, "org": "Ensight", "conv_id": "e", "attr_what": "sg"})
    by = led.rollup("org")
    assert by["Healiom"]["realtime_usd"] == 220 and by["Ensight"]["realtime_usd"] == 10


def test_by_repo_scopes_remote(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "batch", "usd": 26.0, "repo": "charm", "conv_id": "c1", "attr_what": "charm"})
    led.record({"source": "x", "kind": "realtime", "usd": 100.0, "repo": "lmm", "conv_id": "l1", "attr_what": "lmm rt"})
    led.record({"source": "x", "kind": "remote", "usd": 1225.0, "repo": "lmm", "conv_id": "l2", "attr_what": "vast"})
    charm, lmm = led.by_repo("charm"), led.by_repo("lmm")
    assert charm["batch_usd"] == 26 and charm["remote_usd"] == 0   # charm ran NO vast.ai → $0, structurally
    assert charm["billed"] == 26                                   # not polluted by lmm's $1,225 remote
    assert lmm["realtime_usd"] == 100 and lmm["remote_usd"] == 1225


def test_query_filters(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "gate", "kind": "realtime", "usd": 1, "org": "Healiom", "conv_id": "q1", "attr_what": "a"})
    led.record({"source": "reconstruction", "kind": "realtime", "usd": 1, "org": "Ensight", "conv_id": "q2", "attr_what": "b"})
    assert len(led.query(where={"source": "gate"})) == 1
    assert len(led.query(where={"org": "Healiom"})) == 1
    with pytest.raises(ValueError):
        led.query(where={"nonexistent_col": 1})
