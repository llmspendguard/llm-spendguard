"""Offline test for the work-done layer (_period / _repos / build / rollup / cmd) — isolated home.

NO network, NO git shell-out (we monkeypatch _git_commits), NO LLM. _batch_intents reads the local
call_io corpus, which we seed directly via callio._db(). Pairs git commit subjects + batch intents per
(day, project), then rolls them up by day | week | month.
"""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-workdone-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import workdone, callio, config

_real_git_commits = workdone._git_commits        # capture before any monkeypatch (for the real-git test)

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# ─────────────────────────── _period (date bucketing) ───────────────────────────
print("-- _period: day | week | month --")
check("by=day is identity", workdone._period("2026-06-17", "day") == "2026-06-17")
check("by=month truncates to YYYY-MM", workdone._period("2026-06-17", "month") == "2026-06")
# 2026-06-17 is a Wednesday → Monday of that ISO week is 2026-06-15
check("by=week → Monday of that week", workdone._period("2026-06-17", "week") == "2026-06-15")
# 2026-06-15 is itself a Monday → its own period
check("by=week on a Monday is itself", workdone._period("2026-06-15", "week") == "2026-06-15")
# default falls through to day
check("unknown 'by' falls through to day", workdone._period("2026-06-17", "x") == "2026-06-17")


# ─────────────────────────── _repos (config override) ───────────────────────────
print("-- _repos: config override + default fallback --")
# default (no config) → DEFAULT_REPOS is empty; repos MUST be configured via config workdone.repos
defaults = workdone._repos()
check("default repos empty (must be configured)", len(defaults) == 0)

# now write a config that overrides workdone.repos and confirm it wins. _repos() reads
# config.saas_config(), which overlays ~/.spendguard/saas.json — so the override lives there.
config.saas_path().write_text(json.dumps({
    "workdone": {"repos": {"~/code/foo": "fooproj", "/abs/bar": "barproj"}}
}))
repos = workdone._repos()
check("override repos count", len(repos) == 2)
check("override expands ~", os.path.expanduser("~/code/foo") in repos)
check("override maps to project tag", repos[os.path.expanduser("~/code/foo")] == "fooproj")
check("override keeps absolute path as-is", repos["/abs/bar"] == "barproj")


# ─────────────────────────── seed: git commits (monkeypatch) + batch intents (call_io) ──────────
print("-- build/rollup: seeded commits + intents per (day, project) --")

# Point _repos at two fake projects, and monkeypatch _git_commits to return canned commit subjects.
FAKE_REPOS = {"/fake/nlp": "nlp-pipeline", "/fake/vision": "vision-pipeline"}
workdone._repos = lambda: dict(FAKE_REPOS)

_COMMITS = {
    "/fake/nlp": [("2026-06-15", "nlp: build entity index"),
                  ("2026-06-16", "nlp: fix corpus loader"),
                  ("2026-06-17", "nlp: classifier cross-check")],
    "/fake/vision": [("2026-06-16", "vision: segment fix")],
}
workdone._git_commits = lambda repo, since: list(_COMMITS.get(repo, []))

# Seed the call_io corpus directly (this is what _batch_intents reads).
def seed_io(ts, intent, model):
    db = callio._db()
    with callio._lock:
        db.execute(
            "INSERT INTO call_io (id,ts,intent,provider,model,batch,custom_id,prompt,output,in_tok,out_tok,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (callio._uid(), ts, intent, "openai", model, "batch_" + ts + intent, "c1", "p", "o", 1, 1, "test"))
        db.commit()


# entity-extract intent → conv._project_of maps to "nlp-pipeline"; two calls same day same intent → count 2
seed_io("2026-06-15T10:00:00", "entity-extract", "gpt-5.5")
seed_io("2026-06-15T11:00:00", "entity-extract", "gpt-5.5")
# video-caption intent → matches "video"/"caption" → maps to "vision-pipeline"
seed_io("2026-06-16T09:00:00", "video-caption", "claude-opus-4-8")
# unlabeled intent (empty) on a day with no project signal → falls to "" (untagged) in build()
seed_io("2026-06-17T08:00:00", "", "some-model")

SINCE = "2026-06-15"

rows = workdone.build(since=SINCE)
by_key = {(r["day"], r["project"]): r for r in rows}

check("build: nlp-pipeline 2026-06-15 has 1 commit", by_key[("2026-06-15", "nlp-pipeline")]["n_commits"] == 1)
check("build: nlp-pipeline 2026-06-15 counts 2 entity-extract batch calls",
      by_key[("2026-06-15", "nlp-pipeline")]["intents"].get("entity-extract") == 2)
check("build: nlp-pipeline 2026-06-15 n_batch_calls == 2",
      by_key[("2026-06-15", "nlp-pipeline")]["n_batch_calls"] == 2)
check("build: vision 2026-06-16 has 1 commit",
      by_key[("2026-06-16", "vision-pipeline")]["n_commits"] == 1)
check("build: vision 2026-06-16 video-caption batch call attributed",
      by_key[("2026-06-16", "vision-pipeline")]["intents"].get("video-caption") == 1)
# the empty-intent call → _batch_intents COALESCEs '' to '(unlabeled)'; no project signal → "" (untagged)
check("build: unlabeled intent bucketed under untagged ('')",
      "(unlabeled)" in by_key[("2026-06-17", "")]["intents"])
check("build: commit subjects captured", any(c.startswith("nlp: build entity index")
      for c in by_key[("2026-06-15", "nlp-pipeline")]["commits"]))


# ─────────────────────────── rollup by week / month ───────────────────────────
print("-- rollup: by week aggregates the whole ISO week --")
wk = workdone.rollup(since=SINCE, by="week")
# all our days (06-15..06-17) are in the same ISO week → Monday 2026-06-15
nlp_week = next((r for r in wk if r["project"] == "nlp-pipeline" and r["period"] == "2026-06-15"), None)
check("week rollup: nlp-pipeline period exists", nlp_week is not None)
check("week rollup: nlp-pipeline spans 3 active days", nlp_week["active_days"] == 3)
check("week rollup: nlp-pipeline 3 commits across the week", nlp_week["n_commits"] == 3)
check("week rollup: nlp-pipeline batch calls summed (2 entity-extract)",
      nlp_week["n_batch_calls"] == 2)

print("-- rollup: by month --")
mo = workdone.rollup(since=SINCE, by="month")
nlp_month = next((r for r in mo if r["project"] == "nlp-pipeline" and r["period"] == "2026-06"), None)
check("month rollup: period is YYYY-MM", nlp_month is not None and nlp_month["period"] == "2026-06")
check("month rollup: nlp-pipeline 3 commits", nlp_month["n_commits"] == 3)

print("-- rollup: by day (default) keeps day granularity --")
dy = workdone.rollup(since=SINCE)               # by='day' default
days = {(r["period"], r["project"]) for r in dy}
check("day rollup: each day is its own period", ("2026-06-15", "nlp-pipeline") in days
      and ("2026-06-16", "nlp-pipeline") in days and ("2026-06-17", "nlp-pipeline") in days)


# ─────────────────────────── cmd (CLI rendering, no --push) ───────────────────────────
print("-- cmd: prints the roll-up, returns 0 --")
rc = workdone.cmd(["--since", SINCE, "--by", "week"])
check("cmd --by week returns 0", rc == 0)
rc2 = workdone.cmd(["--since", SINCE, "--by", "month"])
check("cmd --by month returns 0", rc2 == 0)
rc3 = workdone.cmd(["--since", SINCE])          # default by=day
check("cmd default (by day) returns 0", rc3 == 0)

# cmd with many commits exercises the "+N more" truncation branch (>8 commits in a bucket)
big = [("2026-06-15", f"nlp: commit {i}") for i in range(12)]
workdone._git_commits = lambda repo, since: (big if repo == "/fake/nlp" else [])
rc4 = workdone.cmd(["--since", SINCE, "--by", "month"])
check("cmd with >8 commits (truncation branch) returns 0", rc4 == 0)


# ─────────────────────────── real _git_commits against a tiny temp git repo ───────────────────────────
print("-- _git_commits: real git on a tiny temp repo (no monkeypatch) --")
import subprocess
gitrepo = tempfile.mkdtemp(prefix="wd-gitrepo-")
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
have_git = True
try:
    subprocess.run(["git", "-C", gitrepo, "init", "-q"], check=True, env=env, timeout=10)
    open(os.path.join(gitrepo, "f.txt"), "w").write("x")
    subprocess.run(["git", "-C", gitrepo, "add", "-A"], check=True, env=env, timeout=10)
    subprocess.run(["git", "-C", gitrepo, "commit", "-q", "-m", "seed: first commit"],
                   check=True, env=env, timeout=10)
except Exception:
    have_git = False

if have_git:
    commits = _real_git_commits(gitrepo, "2000-01-01")    # the real (un-monkeypatched) git path
    check("_git_commits returns (day, subject) tuples", len(commits) == 1)
    check("_git_commits parses the subject", commits[0][1] == "seed: first commit")
    check("_git_commits day looks like YYYY-MM-DD", len(commits[0][0]) == 10 and commits[0][0][4] == "-")
else:
    print("  [skip] git unavailable")

# a non-git directory → git log returns non-zero → [] (the returncode != 0 branch)
notrepo = tempfile.mkdtemp(prefix="wd-notrepo-")
check("_git_commits on a non-repo returns []", _real_git_commits(notrepo, "2000-01-01") == [])


# ─────────────────────────── _repos exception branch (saas_config raises) ───────────────────────────
print("-- _repos: falls back to DEFAULT_REPOS when config raises --")
import spendguard.config as _cfgmod
orig_saas = _cfgmod.saas_config
_cfgmod.saas_config = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    # workdone._repos was monkeypatched above; restore the real one for this check
    import importlib
    importlib.reload(workdone)
    fallback = workdone._repos()
    check("_repos uses DEFAULT_REPOS on config error", len(fallback) == len(workdone.DEFAULT_REPOS))
finally:
    _cfgmod.saas_config = orig_saas


# ─────────────────────────── _batch_intents exception branch ───────────────────────────
print("-- _batch_intents: swallows errors, returns empty on bad db access --")
import spendguard.callio as _callio
orig_db = _callio._db
_callio._db = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
try:
    bi = workdone._batch_intents("2026-06-01")
    check("_batch_intents returns empty mapping on error", len(bi) == 0)
finally:
    _callio._db = orig_db


# ─────────────────────────── cmd --push (monkeypatched saas, no network) ───────────────────────────
print("-- cmd --push: routes to saas.push_workdone (monkeypatched, no network) --")
import spendguard.saas as _saas
_saas.push_workdone = lambda since=None, by="day": {"pushed": 1, "since": since, "by": by}
rc_push = workdone.cmd(["--push", "--by", "week"])
check("cmd --push returns 0 (deterministic, no network)", rc_push == 0)


print(f"\n{'[FAIL]' if failures else 'OK'} test_workdone: {failures} failure(s)")
sys.exit(1 if failures else 0)
