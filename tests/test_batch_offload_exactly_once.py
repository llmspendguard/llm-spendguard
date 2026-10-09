"""EXACTLY-ONCE guard for batch_tracker.submit_offload — a crash between the paid submit and the durable mark/register
must NOT let a re-run create a second paid batch. Surfaced by the honestreview precommit gate 2026-09-28 as a real
reliability hole (submit → mark → register is a non-atomic paid write; a crash in the middle, then lease-reclaim, could
double-submit).

The fix under test: submit_offload stamps every batch at create with a DETERMINISTIC offload key
(metadata sg_offload_key = hash(sorted rows ⋮ intent ⋮ model ⋮ provider)) and, BEFORE paying, reconciles against the
provider's OWN batch list — if a LIVE batch already carries this key it ADOPTS it (idempotent mark + register, no second
submit). The provider's batch list is the durable record, so no local outbox and no trust in a provider idempotency key.

This drives the REAL submit_offload → submit_chat_tasks → guarded_submit → batches.create(metadata=…) path and the REAL
callio.find_live_batch_by_metadata reconcile, with only the OpenAI CLIENT faked — a registry object (created in main and
passed INTO the fake client, never a module global) that create appends to and list/retrieve read. No network, no spend.
models.resolve_effort is stubbed so no live effort probe fires. Isolated SPENDGUARD_HOME.
"""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-batch-offload-exactly-once-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")     # the fake client ignores it; resolves the key lookup
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import openai                                                    # noqa: E402
from spendguard import batch_tracker, lane_queue, callio         # noqa: E402
from spendguard import models as _models                         # noqa: E402

MODEL = "gpt-5-nano"                                             # a real OpenAI batch model (priced) — no source default
INTENT = "batch-offload-exactly-once-test"


class _Registry:
    """The fake provider's batch store. An OBJECT owned by main() and passed into each fake client, so the two clients
    guarded_submit and callio construct share ONE store WITHOUT a module-level global. `created` is the money meter."""
    def __init__(self):
        self.all = []
        self.created = 0

    def add(self, batch):
        self.all.append(batch)
        self.created += 1


class _FakeFile:
    def __init__(self, fid):
        self.id = fid


class _FakeBatch:
    def __init__(self, bid, metadata, status="validating", created_at=None):
        self.id = bid
        self.metadata = dict(metadata or {})
        self.status = status
        self.created_at = int(created_at if created_at is not None else time.time())   # recent → within the recency window
        self.output_file_id = None
        self.input_file_id = None
        self.request_counts = None


class _FakePage:
    def __init__(self, data):
        self.data = data
        self.has_more = False                                    # the whole (small) registry fits in one page


class _FakeBatches:
    def __init__(self, reg):
        self._reg = reg

    def create(self, *, input_file_id, endpoint, completion_window, metadata=None, **kw):
        b = _FakeBatch("batch_test_%d" % (self._reg.created + 1), metadata)
        self._reg.add(b)
        return b

    def list(self, *, limit=100, after=None, **kw):
        return _FakePage(list(reversed(self._reg.all)))         # OpenAI returns most-recent first

    def retrieve(self, bid):
        for b in self._reg.all:
            if b.id == bid:
                return b
        raise KeyError(bid)


class _FakeFiles:
    def create(self, *, file, purpose, **kw):
        return _FakeFile("file_test_1")


class _FakeCompletions:
    def create(self, **kw):
        return type("FakeCompletion", (), {"choices": []})()


class _FakeOpenAI:
    def __init__(self, reg):
        self.batches = _FakeBatches(reg)
        self.files = _FakeFiles()
        self.chat = type("FakeChat", (), {"completions": _FakeCompletions()})()


_seed_counter = [0]


def _seed_leased(n):
    """Enqueue n tasks of INTENT and lease them → n rows in 'leased' state (exactly what the drain hands submit_offload).
    Each call uses a UNIQUE task-text prefix so repeated seedings across cases are INDEPENDENT rows — identical text
    would (correctly) content-address-dedup onto a prior case's rows (see test_queue_content_addressed_dedup), which
    is not what these exactly-once cases model. Returns the leased row dicts {id, intent, task, system, reasoning}."""
    _seed_counter[0] += 1
    prefix = _seed_counter[0]
    lane_queue.enqueue_many(INTENT, ["seed%d-task-%d" % (prefix, i) for i in range(n)])
    return lane_queue.lease(n)


def _inject_noise(reg, n):
    """Append n UNRELATED, recent batches directly into the fake registry (bypassing create, so the money meter is
    untouched) — models many newer batches created on the same account AFTER an orphan, each with a different key."""
    for _ in range(n):
        reg.all.append(_FakeBatch("batch_noise_%d" % len(reg.all), {batch_tracker._OFFLOAD_KEY_FIELD: "unrelated-%d" % len(reg.all)}))


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    reg = _Registry()                                           # owned here; passed into every fake client below
    _real_openai, _real_effort = openai.OpenAI, _models.resolve_effort
    openai.OpenAI = lambda *a, **k: _FakeOpenAI(reg)            # guarded_submit's client AND callio._oai_client share reg
    _models.resolve_effort = lambda *a, **k: None              # omit reasoning_effort → no live probe during build
    try:
        # ── CASE A — the task's core: a re-run on the SAME rows does NOT create a second paid batch (it adopts) ──
        print("-- CASE A: re-run on the same rows adopts, never double-submits --")
        rows = _seed_leased(3)
        r1 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        ck("first offload created a batch", bool(r1.get("batch_id")) and not r1.get("error"), r1)
        ck("exactly one paid batch created", reg.created == 1, reg.created)
        key = batch_tracker._offload_key([r["id"] for r in rows], INTENT, MODEL, "openai")
        ck("the batch is stamped with the deterministic offload key",
           reg.all[0].metadata.get(batch_tracker._OFFLOAD_KEY_FIELD) == key, reg.all[0].metadata)
        r2 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        ck("the re-run ADOPTED the existing batch (same id)", r2.get("adopted") and r2.get("batch_id") == r1["batch_id"], r2)
        ck("STILL exactly one paid batch after the re-run (no duplicate)", reg.created == 1, reg.created)

        # ── CASE B — crash BETWEEN submit and mark: the recovery re-run adopts the orphan, never a second batch ──
        print("\n-- CASE B: crash between submit and mark → recovery adopts, no second paid batch --")
        reg.all, reg.created = [], 0
        rows = _seed_leased(3)
        _real_mark = lane_queue.mark_batched
        lane_queue.mark_batched = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash between submit and mark"))
        crashed = False
        try:
            batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        except RuntimeError:
            crashed = True
        finally:
            lane_queue.mark_batched = _real_mark
        ck("the crash fired AFTER the paid submit (one batch exists, rows unmarked)", crashed and reg.created == 1,
           (crashed, reg.created))
        rec = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)   # lease-reclaim recovery re-run
        ck("recovery ADOPTED the crash-orphaned batch", rec.get("adopted") and rec.get("batch_id") == reg.all[0].id, rec)
        ck("NO second paid batch was created by the recovery", reg.created == 1, reg.created)
        ck("the rows are now marked to the adopted batch", rec.get("marked") == 3, rec)

        # ── CASE C — a TERMINAL (failed) keyed batch is NOT re-adopted; a legitimate re-offer submits fresh ──
        print("\n-- CASE C: a dead (failed) keyed batch is skipped → re-offer submits fresh --")
        reg.all, reg.created = [], 0
        rows = _seed_leased(3)
        r1 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        reg.all[0].status = "failed"                            # the batch went terminal-bad (its rows would fall back)
        r2 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        ck("a failed keyed batch is NOT adopted", not r2.get("adopted"), r2)
        ck("the re-offer created a SECOND, distinct batch", reg.created == 2 and r2.get("batch_id") != r1["batch_id"],
           (reg.created, r1.get("batch_id"), r2.get("batch_id")))

        # ── CASE D — different row-sets get different keys → distinct batches (no FALSE adoption) ──
        print("\n-- CASE D: different rows → different key → distinct batches --")
        reg.all, reg.created = [], 0
        rows_a = _seed_leased(2)
        rows_b = _seed_leased(2)                                 # different row ids
        ra = batch_tracker.submit_offload(INTENT, rows_a, MODEL, cap_dollars=5.0)
        rb = batch_tracker.submit_offload(INTENT, rows_b, MODEL, cap_dollars=5.0)
        ck("a different row-set is NOT adopted onto the first batch",
           not rb.get("adopted") and rb.get("batch_id") != ra.get("batch_id"), (ra, rb))
        ck("two distinct paid batches for two distinct row-sets", reg.created == 2, reg.created)

        # ── CASE E — the hook's scenario: the orphan is found even behind MANY newer batches (no COUNT-cap miss) ──
        print("\n-- CASE E: orphan found behind 250 newer batches (recent-first scan to list-end, never count-capped) --")
        reg.all, reg.created = [], 0
        rows = _seed_leased(2)
        r1 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)   # the orphan (created first = oldest)
        _inject_noise(reg, 250)                                  # 250 newer unrelated batches — well past the old 200-cap
        ck("the orphan now sits behind 250 newer batches", len(reg.all) == 251, len(reg.all))
        r2 = batch_tracker.submit_offload(INTENT, rows, MODEL, cap_dollars=5.0)
        ck("the orphan is STILL found and adopted (a count cap here would have missed it → duplicate)",
           r2.get("adopted") and r2.get("batch_id") == r1["batch_id"], r2)
        ck("no duplicate paid batch was created", reg.created == 1, reg.created)

        # ── CASE F — an un-completable scan RAISES, never a silent None that would authorize a fresh submit ──
        print("\n-- CASE F: a scan that can't reach the recency boundary RAISES (never a truncated 'none') --")
        reg.all, reg.created = [], 0
        _inject_noise(reg, 10)                                   # 10 unrelated batches, none matching the sought key
        _real_ceiling = callio._RECONCILE_SCAN_CEILING
        callio._RECONCILE_SCAN_CEILING = 3                       # force the scan to exceed the ceiling before finishing
        raised = False
        try:
            # no match anywhere → the scan must hit the ceiling and RAISE, never return a truncated None
            callio.find_live_batch_by_metadata(batch_tracker._OFFLOAD_KEY_FIELD, "does-not-exist")
        except RuntimeError:
            raised = True
        finally:
            callio._RECONCILE_SCAN_CEILING = _real_ceiling
        ck("a scan past the ceiling RAISES (a truncated scan is never concluded as 'none' → no duplicate)", raised)

        # ── CASE G — a MATCHED live batch with no id RAISES (an id-less match is never read as 'none') ──
        print("\n-- CASE G: a matched live batch with no id RAISES (never a silent None that authorizes a fresh submit) --")
        reg.all, reg.created = [], 0
        reg.all.append(_FakeBatch(None, {batch_tracker._OFFLOAD_KEY_FIELD: "seek-key"}))   # matches key + live, but NO id
        raised_noid = False
        try:
            callio.find_live_batch_by_metadata(batch_tracker._OFFLOAD_KEY_FIELD, "seek-key")
        except RuntimeError:
            raised_noid = True
        ck("an id-less matched live batch RAISES (never concluded as 'none' → no duplicate)", raised_noid)
    finally:
        openai.OpenAI, _models.resolve_effort = _real_openai, _real_effort

    print(f"\n{'[FAIL]' if fails else 'OK'} test_batch_offload_exactly_once: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
