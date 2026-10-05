"""File-backed mock of the OpenAI chat Batch API for the durable-batch tests (NOT a test — underscore-prefixed). A
submitted 'batch' is recorded in a FILE under SPENDGUARD_HOME, so it PERSISTS across a SIGKILL (the whole point of the
crash test: prove the state is in a durable store, not RAM). install() monkeypatches:
  · submit.submit_chat_tasks      — create a batch (count creates) keyed by the exactly-once sg_offload_key, store
                                     {custom_id: canary} so collect can echo it; return {batch_id};
  · callio.collect_chat_tasks     — read the file, return {results: {custom_id: 'ok <canary>'}} (ready immediately);
  · batch_tracker._existing_offload_batch — reconcile: if a batch with this key already exists, return it (ADOPT),
                                     which is how a crash-retry of the SAME rows avoids a second paid create.
`creates()` reports how many batches were actually created — the double-spend guard.
"""
import json
import os
import re
import threading

from spendguard import batch_tracker as BT, callio as C, submit as S

_OFFLOAD_FIELD = "sg_offload_key"
_lk = threading.Lock()


def _path():
    return os.path.join(os.environ["SPENDGUARD_HOME"], "mockprov.json")


def _load():
    try:
        with open(_path()) as fh:
            return json.load(fh)
    except Exception:
        return {"creates": 0, "batches": {}}               # batches: bid -> {"key": key, "items": {cid: canary}}


def _save(d):
    with open(_path(), "w") as fh:
        json.dump(d, fh)


def _canary(s):
    m = re.search(r"CANARY-\d+", str(s or ""))
    return m.group(0) if m else None


def install():
    def _submit_chat(tasks, model, **kw):
        key = (kw.get("metadata") or {}).get(_OFFLOAD_FIELD)
        with _lk:
            d = _load()
            d["creates"] += 1
            bid = "mock-b%d" % d["creates"]
            d["batches"][bid] = {"key": key, "items": {t["custom_id"]: _canary(t["content"]) for t in tasks}}
            _save(d)
        return {"batch_id": bid, "error": None}

    def _collect_chat(batch_ids, intent, model, require_ready=True, record_io=False):
        if isinstance(batch_ids, str):
            batch_ids = [batch_ids]
        with _lk:
            d = _load()
        results = {}
        for bid in batch_ids:
            for cid, can in (d["batches"].get(bid, {}).get("items") or {}).items():
                results[str(cid)] = "ok %s" % can
        return {"results": results, "failed": {}, "not_ready": [], "collected": len(results), "batches": len(batch_ids)}

    def _existing(key, provider):
        with _lk:
            d = _load()
        for bid, b in d["batches"].items():
            if b.get("key") == key:
                return bid                                  # a live batch with this key exists → ADOPT (no 2nd create)
        return None

    S.submit_chat_tasks = _submit_chat
    C.collect_chat_tasks = _collect_chat
    BT._existing_offload_batch = _existing


def creates():
    with _lk:
        return _load().get("creates", 0)
