"""Offline test for bootstrap — the cold-start process. bootstrap(run=False) is a DRY RUN: it must
NOT spend a cent. Every network/LLM-touching step is stubbed; the paid reasoning step (6/6) is asserted
to be called with run=False, and we verify the meta ledger stays at $0 across the whole dry run.
"""
import atexit, os, shutil, sys, tempfile

BOOTSTRAP_OWNED_HOME_ENV = "SPENDGUARD_BOOTSTRAP_OWNED_HOME"
BOOTSTRAP_HOME_PREFIX = "spendguard-bootstrap-"
BOOTSTRAP_FAKE_REPOSITORY_PREFIX = "fake-repo-"
BOOTSTRAP_FAKE_TRANSCRIPTS_PREFIX = "fake-transcripts-"


def _bootstrap_owned_temp_dir(prefix):
    owned_path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, owned_path, ignore_errors=True)
    return owned_path

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix=BOOTSTRAP_HOME_PREFIX)
    os.environ[BOOTSTRAP_OWNED_HOME_ENV] = os.environ["SPENDGUARD_HOME"]
    os.execv(sys.executable, [sys.executable] + sys.argv)
bootstrap_owned_home = os.environ.pop(BOOTSTRAP_OWNED_HOME_ENV, None)
if bootstrap_owned_home:
    atexit.register(shutil.rmtree, bootstrap_owned_home, ignore_errors=True)

from spendguard import bootstrap, backfill, history, callio, conv, review, advisor, budget

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# ── record what each step received; stub every network/LLM path (zero spend) ──
calls_log = {}


def _spy(name, ret=None):
    def fn(*a, **k):
        calls_log[name] = dict(args=a, kwargs=k)
        return ret
    return fn


# free-recovery steps (1-5): stubbed offline, deterministic
backfill.backfill = _spy("backfill", ret=(3, 22.75))
history.reconstruct_intents = _spy("reconstruct_intents")
history.enrich_graph = _spy("enrich_graph")
callio.fetch_history = _spy("fetch_history", ret={"added": 4, "batches_fetched": 2, "errors": 0})
conv.index_cmd = _spy("index_cmd")

# paid reasoning step (6/6): MUST be invoked with run=False on a dry run, MUST NOT spend
review.review = _spy("review", ret={"requests": 1, "cost": 0.01})
advisor.mine = _spy("mine", ret={"requests": 1, "cost": 0.01})
conv.synth = _spy("synth", ret={"requests": 1, "cost": 0.01})
advisor.reconstruct = _spy("reconstruct", ret={"requests": 0, "cost": 0.0})

repo = _bootstrap_owned_temp_dir(BOOTSTRAP_FAKE_REPOSITORY_PREFIX)
tdir = _bootstrap_owned_temp_dir(BOOTSTRAP_FAKE_TRANSCRIPTS_PREFIX)

print("-- bootstrap(run=False): DRY RUN completes, returns 0, spends nothing --")
meta_before = budget.meta_spent_today()
rc = bootstrap.bootstrap(repo=repo, transcripts=tdir, run=False, cap=10)
check("returns 0", rc == 0)
check("free step 1 (backfill) called", "backfill" in calls_log)
check("free step 2 (reconstruct_intents) called with repo + apply=True",
      calls_log["reconstruct_intents"]["args"][0] == repo
      and calls_log["reconstruct_intents"]["kwargs"].get("apply") is True)
check("free step 3 (enrich_graph) called", "enrich_graph" in calls_log)
check("free step 4 (fetch_history) called with cap=10",
      calls_log["fetch_history"]["kwargs"].get("cap") == 10)
check("free step 5 (index_cmd) called with transcripts + apply=True",
      calls_log["index_cmd"]["args"][0] == tdir
      and calls_log["index_cmd"]["kwargs"].get("apply") is True)

print("-- paid step (6/6) reasoners all invoked with run=False --")
check("review run=False", calls_log["review"]["kwargs"].get("run") is False)
check("mine run=False", calls_log["mine"]["kwargs"].get("run") is False)
check("synth run=False", calls_log["synth"]["kwargs"].get("run") is False)
check("reconstruct run=False", calls_log["reconstruct"]["kwargs"].get("run") is False)

print("-- ZERO SPEND invariant: meta ledger unchanged across the dry run --")
check("meta_spent_today unchanged", abs(budget.meta_spent_today() - meta_before) < 1e-12)
check("no workload spend recorded", budget.spent_today() == 0.0)

print("-- free steps that RAISE are caught (skipped, never fatal) --")
def boom(*a, **k):
    raise RuntimeError("network down")
backfill.backfill = boom
history.reconstruct_intents = boom
history.enrich_graph = boom
callio.fetch_history = boom
conv.index_cmd = boom
# reasoners still no-op (run=False)
rc2 = bootstrap.bootstrap(repo=repo, transcripts=tdir, run=False)
check("dry run still returns 0 when free steps fail", rc2 == 0)
check("still zero spend after failures", budget.spent_today() == 0.0)

print("-- bootstrap default repo = cwd when none given --")
# restore a benign backfill so step 1 records the repo arg path
backfill.backfill = _spy("backfill", ret=(0, 0.0))
history.reconstruct_intents = _spy("reconstruct_intents")
history.enrich_graph = _spy("enrich_graph")
callio.fetch_history = _spy("fetch_history", ret={"added": 0, "batches_fetched": 0, "errors": 0})
conv.index_cmd = _spy("index_cmd")
calls_log.pop("reconstruct_intents", None)
rc3 = bootstrap.bootstrap(run=False)
check("returns 0 with default repo", rc3 == 0)
check("reconstruct_intents got a repo path (cwd default)",
      bool(calls_log.get("reconstruct_intents", {}).get("args")))

print("-- bootstrap(run=True): spend-sensitive negative control proves the dry-run oracle is non-vacuous --")
# Negative control: the same paid-step double records a charge only when run=True. This proves the
# conservation assertion above is sensitive to spend instead of passing vacuously against inert stubs.
negative_control_meta_cost = 0.01


def _ledger_sensitive_review(*a, **k):
    calls_log["review"] = dict(args=a, kwargs=k)
    if k.get("run") is True:
        budget.record_meta(__name__, _ledger_sensitive_review.__name__, negative_control_meta_cost)
    return {"insights": 1, "cost": negative_control_meta_cost if k.get("run") is True else 0.0}


review.review = _ledger_sensitive_review
advisor.mine = _spy("mine", ret={"insights": 1, "cost": 0.0})
conv.synth = _spy("synth", ret={"insights": 1, "cost": 0.0})
advisor.reconstruct = _spy("reconstruct", ret={"judged": 0, "cost": 0.0})
negative_control_meta_before = budget.meta_spent_today()
rc_run = bootstrap.bootstrap(repo=repo, transcripts=tdir, run=True)
check("run=True returns 0", rc_run == 0)
check("run=True forwarded to review", calls_log["review"]["kwargs"].get("run") is True)
check("run=True forwarded to reconstruct", calls_log["reconstruct"]["kwargs"].get("run") is True)
check("negative control records meta spend when run=True",
      budget.meta_spent_today() > negative_control_meta_before)
# restore dry-run no-ops for the CLI test below
review.review = _spy("review", ret={"requests": 1, "cost": 0.0})
advisor.mine = _spy("mine", ret={"requests": 1, "cost": 0.0})
conv.synth = _spy("synth", ret={"requests": 1, "cost": 0.0})
advisor.reconstruct = _spy("reconstruct", ret={"requests": 0, "cost": 0.0})

print("-- main(argv): dry-run CLI path returns 0 (no --run) --")
rc4 = bootstrap.main(["--repo", repo, "--transcripts", tdir, "--cap", "5"])
check("main dry-run returns 0", rc4 == 0)
check("main passed cap=5 to fetch_history", calls_log["fetch_history"]["kwargs"].get("cap") == 5)
check("main stayed zero spend", budget.spent_today() == 0.0)

print(f"\n{'[FAIL]' if failures else 'OK'} bootstrap: {failures} failure(s)")
sys.exit(1 if failures else 0)
