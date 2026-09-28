"""C2 — thoughtful batch CHUNKING sizes each Batch-API chunk to the tightest REAL constraint.

Offline + deterministic ($0, no provider call): asserts plan_batch_chunks splits a backlog by the MIN of the
provider's published cap, the MB-at-avg-tokens cap (so a chunk never 413s), the account's enqueued-token limit, and
Ash's validated STAGE ceiling (never-large-batches) — and NAMES the binding constraint so the plan is auditable. The
provider caps are the real published numbers (OpenAI 50k/200MB, Anthropic 100k/256MB), config-overridable.
"""
import json, os, pathlib, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-qchunk-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# an account-specific enqueued-token limit for moonshot (both providers say "see platform settings") — config-driven
pathlib.Path(os.environ["SPENDGUARD_HOME"], "config.json").write_text(
    json.dumps({"batch": {"max_enqueued_tokens": {"moonshot": 90000}}}))

from spendguard import queue_planner   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = Checks()
    print("== published provider Batch-API caps (grounded from docs), config-overridable ==")
    c.ck("openai → 50,000 req / 200 MB", queue_planner.batch_caps("openai")["max_requests"] == 50000
         and queue_planner.batch_caps("openai")["max_mb"] == 200, str(queue_planner.batch_caps("openai")))
    c.ck("anthropic → 100,000 req / 256 MB", queue_planner.batch_caps("anthropic")["max_requests"] == 100000
         and queue_planner.batch_caps("anthropic")["max_mb"] == 256, str(queue_planner.batch_caps("anthropic")))
    c.ck("unknown vendor → conservative fallback (50k/200)", queue_planner.batch_caps("zzz")["max_requests"] == 50000)
    c.ck("account enqueued-token limit is read from config", queue_planner.batch_caps("moonshot")
         .get("max_enqueued_tokens") == 90000, str(queue_planner.batch_caps("moonshot")))

    print("\n== the VALIDATED STAGE ceiling binds by default (never-large-batches → bounded blast radius) ==")
    p = queue_planner.plan_batch_chunks(2500, "openai", avg_call_tokens=3000)
    c.ck("stage cap (1000) binds — far under the 50k provider cap", p["binding"] == "stage_cap"
         and p["chunk_size"] == 1000, str({"binding": p["binding"], "size": p["chunk_size"]}))
    c.ck("chunks tile 2500 → [1000, 1000, 500]", p["chunks"] == [1000, 1000, 500] and p["n_chunks"] == 3,
         str(p["chunks"]))
    c.ck("a caller-passed smaller stage is honored", queue_planner.plan_batch_chunks(
        2500, "openai", avg_call_tokens=3000, stage_cap=250)["chunk_size"] == 250)

    print("\n== the MB cap binds for huge prompts (a chunk that would 413 is split smaller) ==")
    # openai 200 MB / (200,000 tok * 4 bytes) = 250 requests fit the MB cap → it binds below the 1000 stage
    p2 = queue_planner.plan_batch_chunks(2500, "openai", avg_call_tokens=200000)
    c.ck("MB cap binds at 250 for 200k-token calls", p2["binding"] == "mb_cap" and p2["chunk_size"] == 250, str(p2))

    print("\n== the account enqueued-token limit binds tightest when set ==")
    # moonshot enqueued 90,000 / 3,000 tok = 30 requests → binds below stage(1000) and mb
    p3 = queue_planner.plan_batch_chunks(2500, "moonshot", avg_call_tokens=3000)
    c.ck("enqueued-token cap binds at 30 (90000/3000)", p3["binding"] == "enqueued_token_cap"
         and p3["chunk_size"] == 30, str({"binding": p3["binding"], "size": p3["chunk_size"]}))
    c.ck("chunks tile 2500 at 30 → 84 chunks summing to 2500", p3["n_chunks"] == 84 and sum(p3["chunks"]) == 2500,
         str({"n_chunks": p3["n_chunks"], "sum": sum(p3["chunks"])}))

    print("\n== edge: empty backlog → no chunks ==")
    c.ck("n=0 → no chunks", queue_planner.plan_batch_chunks(0, "openai")["chunks"] == [])

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_planner_chunking: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
