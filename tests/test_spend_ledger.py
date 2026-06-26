"""SpendLedger foundation tests — proven BEFORE any consumer hooks up.

Financial-systems grade: integer micro-money (exact), UTC time + accounting day/period, append-only hash chain
(tamper-evident), cost routing, dedup (no double-count), per-repo scoping, billed-vs-est_value split, meta exclusion.
"""
import pytest
from spendguard.ledger import SpendLedger, MICRO_COLS, LockedError


def _led(tmp_path):
    return SpendLedger(db_path=str(tmp_path / "ledger.db"))


# ── Step 1: schema + record/get ──

def test_cost_routes_to_correct_micros_column_and_json_roundtrips(tmp_path):
    led = _led(tmp_path)
    eid = led.record({"source": "reconstruction", "kind": "realtime", "usd": 220.0,
                      "provider": "openai", "model": "gpt-5.5", "model_kind": "completion",
                      "org": "Healiom", "team": "lmm", "projects": ["lmm", "medical-taxonomy"],
                      "cwd": "/Users/x/Documents/claude/lmm", "conv_id": "c1", "batch_id": "b1",
                      "cost_basis": "printed", "from_message_ids": ["m1", "m2"], "tags": ["realtime"],
                      "attr_what": "loinc stem pass", "attr_why": "cwd=lmm", "attr_how": "cwd-match"})
    ev = led.get(eid)
    assert ev["realtime_micros"] == 220_000_000
    assert ev["batch_micros"] == 0 and ev["est_chat_micros"] == 0 and ev["remote_compute_micros"] == 0
    assert ev["projects"] == ["lmm", "medical-taxonomy"]
    assert ev["from_message_ids"] == ["m1", "m2"] and ev["tags"] == ["realtime"]
    assert ev["org"] == "Healiom" and ev["attr_how"] == "cwd-match"


@pytest.mark.parametrize("kind,col", [("batch", "batch_micros"), ("realtime", "realtime_micros"),
                                      ("est_chat", "est_chat_micros"), ("remote", "remote_compute_micros"),
                                      ("subscription", "subscription_micros")])
def test_every_kind_routes_to_its_own_micros_column(tmp_path, kind, col):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "s", "kind": kind, "usd": 5.0, "conv_id": kind}))
    assert ev[col] == 5_000_000
    assert sum(ev[c] for c in MICRO_COLS) == 5_000_000


def test_dedup_same_evidence_records_once(tmp_path):
    led = _led(tmp_path)
    base = {"source": "reconstruction", "kind": "realtime", "usd": 220.0,
            "provider": "openai", "model": "gpt-5.5", "conv_id": "c1", "batch_id": "b1", "attr_what": "loinc"}
    a = led.record(base)
    b = led.record({**base, "org": "Healiom"})
    assert a == b
    assert led._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0] == 1


def test_invalid_events_rejected(tmp_path):
    led = _led(tmp_path)
    with pytest.raises(ValueError):
        led.record({"source": "x", "provider": "openai"})
    with pytest.raises(ValueError):
        led.record({"source": "x", "kind": "weird", "usd": 1.0})
    with pytest.raises(ValueError):
        led.record({"kind": "realtime", "usd": 1.0})


# ── money: integer micros are EXACT (float would drift) ──

def test_micros_are_exact_where_float_drifts(tmp_path):
    led = _led(tmp_path)
    for i in range(3):
        led.record({"source": "x", "kind": "realtime", "usd": 0.1, "conv_id": f"c{i}", "attr_what": f"a{i}"})
    t = led.rollup()
    assert t["realtime_micros"] == 300_000     # exact
    assert t["realtime_usd"] == 0.3
    assert 0.1 + 0.1 + 0.1 != 0.3              # the float trap this avoids


# ── rollup: billed vs est-value split, per-org, per-repo, meta exclusion ──

def test_rollup_billed_vs_estvalue_split(tmp_path):
    led = _led(tmp_path)
    for k, u, c in [("batch", 100, "a"), ("realtime", 5, "b"), ("remote", 20, "c"),
                    ("est_chat", 50, "d"), ("subscription", 400, "s")]:
        led.record({"source": "x", "kind": k, "usd": u, "conv_id": c, "attr_what": k})
    t = led.rollup()
    assert (t["batch_usd"], t["realtime_usd"], t["remote_compute_usd"], t["est_chat_usd"], t["subscription_usd"]) \
        == (100, 5, 20, 50, 400)
    assert t["billed_usd"] == 525        # batch+realtime+remote+subscription — NEVER est_chat
    assert t["est_value_usd"] == 50
    assert t["n"] == 5


def test_rollup_by_org(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "realtime", "usd": 220.0, "org": "Healiom", "conv_id": "h", "attr_what": "loinc"})
    led.record({"source": "x", "kind": "realtime", "usd": 10.0, "org": "Ensight", "conv_id": "e", "attr_what": "sg"})
    by = led.rollup("org")
    assert by["Healiom"]["realtime_usd"] == 220 and by["Ensight"]["realtime_usd"] == 10


def test_by_repo_scopes_remote(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "batch", "usd": 26.0, "repo": "charm", "conv_id": "c1", "attr_what": "charm"})
    led.record({"source": "x", "kind": "realtime", "usd": 100.0, "repo": "lmm", "conv_id": "l1", "attr_what": "lmm"})
    led.record({"source": "x", "kind": "remote", "usd": 1225.0, "repo": "lmm", "conv_id": "l2", "attr_what": "vast"})
    charm, lmm = led.by_repo("charm"), led.by_repo("lmm")
    assert charm["batch_usd"] == 26 and charm["remote_compute_usd"] == 0   # charm ran NO vast.ai → $0
    assert charm["billed_usd"] == 26
    assert lmm["realtime_usd"] == 100 and lmm["remote_compute_usd"] == 1225


def test_meta_excluded_from_workload_rollup(tmp_path):
    led = _led(tmp_path)
    led.record({"source": "x", "kind": "realtime", "usd": 10.0, "conv_id": "w", "attr_what": "work"})
    led.record({"source": "x", "kind": "realtime", "usd": 5.0, "conv_id": "m", "is_meta": 1, "attr_what": "usage pull"})
    assert led.rollup()["realtime_usd"] == 10.0
    assert led.rollup(include_meta=True)["realtime_usd"] == 15.0


# ── time: UTC canonical, transaction date vs posting date, reporting-tz accounting day ──

def test_timestamps_are_utc_with_defaults(tmp_path):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "x", "kind": "realtime", "usd": 1.0, "conv_id": "c", "attr_what": "a"}))
    assert ev["ts_utc"].endswith("+00:00")           # tz-aware UTC
    assert ev["recorded_at"] and ev["occurred_at"]
    assert ev["currency"] == "USD" and ev["status"] == "draft"   # newly ingested


def test_occurred_vs_recorded_accounting_day(tmp_path):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "reconstruction", "kind": "realtime", "usd": 1.0, "conv_id": "c",
                             "occurred_at": "2026-05-15T10:00:00+00:00", "attr_what": "loinc"}))
    assert ev["day"] == "2026-05-15" and ev["period"] == "2026-05"   # accounting day from the TRANSACTION date
    assert ev["recorded_at"] != ev["occurred_at"]                    # booked later than it happened


# ── lifecycle & controls (Xero/Intuit): mutable until locked, audit-logged, hash-chained ──

def test_update_logs_to_audit_and_bumps_revision(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "gate", "kind": "realtime", "usd": 1.0, "conv_id": "c", "attr_what": "x"})
    assert led.get(a)["status"] == "draft" and led.get(a)["revision"] == 1
    n = led.update(a, {"org": "Healiom", "status": "posted", "attr_how": "cwd-match"}, actor="attr", reason="cwd=lmm")
    assert n == 3                                       # three fields changed
    ev = led.get(a)
    assert ev["org"] == "Healiom" and ev["status"] == "posted" and ev["revision"] == 2
    hist = led.history(a)
    assert hist[0]["pass"] == "ingest"                 # creation logged first
    assert any(h["field"] == "org" and h["new_value"] == "Healiom" and h["actor"] == "attr" for h in hist)


def test_protected_fields_cannot_be_updated(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "gate", "kind": "realtime", "usd": 1.0, "conv_id": "c", "attr_what": "x"})
    with pytest.raises(ValueError):
        led.update(a, {"period": "2099-01"})           # identity/period is immutable → reverse/adjust


def test_lock_refuses_update_and_new_post_then_reverse(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "gate", "kind": "realtime", "usd": 10.0, "conv_id": "c",
                    "occurred_at": "2026-05-10T00:00:00+00:00", "attr_what": "x"})
    assert led.lock_period("2026-05", reason="close May", actor="ash") == 1
    assert led.get(a)["status"] == "locked"
    with pytest.raises(LockedError):
        led.update(a, {"org": "Healiom"})              # locked row is immutable
    with pytest.raises(LockedError):
        led.record({"source": "gate", "kind": "realtime", "usd": 1.0, "conv_id": "late",
                    "occurred_at": "2026-05-11T00:00:00+00:00", "attr_what": "late"})   # no posting into a closed period
    rid = led.reverse(a, actor="ash", reason="wrong")  # correction = reversal into the open period
    rev = led.get(rid)
    assert rev["realtime_micros"] == -10_000_000 and rev["reverses_id"] == a
    assert led.get(a)["realtime_micros"] == 10_000_000  # original untouched


def test_reversed_pair_nets_to_zero(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "gate", "kind": "realtime", "usd": 10.0, "conv_id": "c",
                    "occurred_at": "2026-05-10T00:00:00+00:00", "attr_what": "x"})
    led.lock_period("2026-05")
    led.reverse(a)
    assert led.rollup()["realtime_usd"] == 0.0          # +10 (May) and −10 (reversal) net out


def test_audit_chain_verifies_and_detects_tamper(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "x", "kind": "realtime", "usd": 1.0, "conv_id": "c1", "attr_what": "a"})
    led.update(a, {"org": "Healiom"}, actor="attr", reason="cwd=lmm")
    ok, bad = led.verify_audit_chain()
    assert ok and bad is None
    led._conn.execute("UPDATE spend_audit SET new_value='Ensight' WHERE event_id=? AND field='org'", (a,))
    led._conn.commit()
    ok, bad = led.verify_audit_chain()
    assert not ok                                       # the immutable log detects the alteration


# ── rate snapshot + FLOAT confidence ──

def test_rate_snapshot_from_price_book(tmp_path):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "gate", "kind": "realtime", "usd": 1.0,
                             "provider": "openai", "model": "gpt-5.5", "conv_id": "rt", "attr_what": "x"}))
    assert ev["rate_in"] is not None and ev["rate_out"] is not None


def test_confidence_is_float(tmp_path):
    led = _led(tmp_path)
    ev = led.get(led.record({"source": "x", "kind": "realtime", "usd": 1.0, "conv_id": "f",
                             "amount_confidence": 0.85, "attr_confidence": 0.9, "attr_what": "z"}))
    assert ev["amount_confidence"] == 0.85 and ev["attr_confidence"] == 0.9


# ── Step 3a: the attribution pass (deterministic plumbing the agentic determiner feeds) ──

def test_attribute_pass_posts_and_logs(tmp_path):
    led = _led(tmp_path)
    a = led.record({"source": "reconstruction", "kind": "realtime", "usd": 220.0, "conv_id": "c",
                    "cwd": "/Users/x/Documents/claude/lmm", "attr_what": "loinc stem pass"})
    assert led.get(a)["status"] == "draft" and led.get(a)["org"] is None
    led.attribute(a, org="Healiom", team="lmm", projects=["lmm"], attr_how="cwd-match",
                  attr_why="cwd=lmm", attr_confidence=0.95, attr_source="ledger-attr", actor="attr-v1")
    ev = led.get(a)
    assert ev["org"] == "Healiom" and ev["team"] == "lmm" and ev["projects"] == ["lmm"]
    assert ev["status"] == "posted" and ev["attr_how"] == "cwd-match" and ev["attr_confidence"] == 0.95
    assert any(h["pass"] == "attribute" and h["field"] == "org" and h["actor"] == "attr-v1" for h in led.history(a))
