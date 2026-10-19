"""A remediation the receipt prints must RUN, and must match the cap it is printed beside.

WHY THIS GUARD EXISTS. The truncation block told the truth about the money and lied about the fix. For every
truncating model it printed:

    spendguard maxtokens --model gemini-3.8-flash   (gate.autotune=apply raises the cap automatically)

Both halves were wrong, and neither failed loudly. `maxtokens` has no `--model` flag — it takes a call-class
SIG, an opaque hash of model+intent — so the command exits on a usage line, and there is no path from a model
name to the sig it wants. Then the parenthesis promised a fix that does not apply: measured on the real
ledger, of 8,574 recorded truncations

    4,299  sat at the model's PUBLISHED CEILING   autotune computes a target, sees target <= cap, returns
    3,179  carried NO cap at all                  autotune never adds a cap where the caller set none
      586  were genuinely raisable                autotune acts — about 7% of the total

so the headline remedy addressed a fourteenth of what it was attached to, and the largest class was not a cap
problem at all. gemini-3.8-flash truncated 4,299 times with its cap ALREADY at the published 65,536 and a
visible answer of 0-9 tokens: reasoning consumed the output budget. There is no higher cap to move to, and the
receipt told you to go get one.

The failure mode is the one this repo keeps naming: a loop that reads as closed. The leak was reported, a fix
was printed beside it, and nobody re-checked that the fix ran — so the waste kept billing with remediation
text sitting next to it. A wrong command is better than this only because a wrong command gets noticed.

So this guard asserts the two things prose cannot: the LABEL matches the cap the class truncated at, and the
COMMAND actually executes without a usage error.
"""
import os, sys, tempfile

# ISOLATED HOME, re-exec'd before spendguard is imported — this test SEEDS observations, and synthetic
# truncating classes in the real gate_calls would make the shipped estimator predict production work from a
# fixture. Same reason the autotune guard next door isolates.
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-truncremedy-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import contextlib, io, sqlite3, subprocess                                      # noqa: E402

from spendguard import bulkgate, config, expected_output, receipt               # noqa: E402

# How long the executed-command check waits on a child interpreter. A hung child would otherwise hang the
# whole suite, and an unbounded wait is the defect this repo's own external-call doctrine blocks at write time.
CHILD_TIMEOUT_SECONDS = 120

# The models are chosen for their PUBLISHED CEILINGS, which the classification reads — not for their names.
# Each case RESOLVES its ceiling from the catalogue and refuses if the catalogue cannot pose it, so a
# catalogue change makes this test say "I could not test that" rather than silently inverting a case.
AT_CEILING_MODEL = "gemini-3.8-flash"
RAISABLE_MODEL = "claude-sonnet-4-6"
NO_CAP_MODEL = "gpt-5-mini"

# The real shape of the raisable class on this ledger: a judge bounded to a few hundred output tokens.
BOUNDED_JUDGE_CAP = 220


class Checks:
    """An explicit accumulator, PASSED to each case rather than reached for.

    A module-level counter mutated through `global` is the pattern the sibling guards use, and it is the one
    this repo's own coding doctrine blocks: state assigned from inside a function, so a second invocation in
    one process carries the first one's failures and the exit code stops describing this run. Holding the
    count in an object that every case receives makes that impossible instead of merely unlikely.

    ADJUDICATED against the duplication enforcer, which flagged `that` as a copy of
    `test_cat7_guard_completeness.ck`. It is a PROTOCOL, not drift, and the count is the evidence: 454 of the
    standalone test programs here define their own check-style helper (351 `ck`, 103 `check`) and
    `tests/conftest.py` offers none, because each test is a self-contained program by design — that is why
    they are programs rather than pytest cases. Consolidating would mean a test importing from another test
    file. Per this repo's naming doctrine, same-name/same-interface across many implementors is correct and
    must not be "fixed"; the `ceiling_of` finding from the same run WAS real and was consolidated.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.ran = 0
        self.unposed: list = []

    def that(self, label: str, ok: bool, extra: str = "") -> bool:
        self.ran += 1
        if not ok:
            self.failures += 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))
        return ok

    def cannot_pose(self, case: str, why: str) -> None:
        """A case whose PRECONDITION is absent — reported as its own outcome, never as a pass or a fail.

        The first version of this file recorded a FAILURE when the catalogue could not supply a ceiling, and
        the enforcer was right to refuse it. "The code is wrong" and "I could not ask the question" are
        different findings and one counter cannot hold both: a failure here sends someone hunting a defect in
        `truncation_remedy` that is not there, while a silent pass would claim coverage the run never had.
        This repo's own axis doctrine states it directly — every axis must report UNREVIEWED separately from
        a clean result, with a denominator. So an unposed case is NAMED, is listed in the verdict line, and
        makes the run exit non-zero, because a guard that quietly stopped guarding is the failure mode the
        whole file exists to prevent.
        """
        self.unposed.append(f"{case}: {why}")
        print(f"  [UNPOSED] {case} — {why}")


def ceiling_of(model: str) -> int:
    """This model's published output ceiling as an int, or 0 when the catalogue does not know it.

    DELEGATES to `expected_output._model_ceiling` rather than wrapping `pricing.max_output_tokens` again —
    the enforcer caught the first version doing the latter, correctly: "safe published ceiling lookup" is one
    job and it already had one implementation. Only the None -> 0 normalisation lives here, because these
    cases branch on an int.
    """
    return int(expected_output._model_ceiling(model) or 0)


def seed(sig: str, model: str, cap: int, sizes, finish: str) -> None:
    """Record responses through the REAL recorder, so the rows are shaped exactly as production writes them."""
    with contextlib.redirect_stderr(io.StringIO()):
        for size in sizes:
            bulkgate.note_response(sig, model, size, cap, finish)


def connection():
    return sqlite3.connect(config.db_path())


def case_a_cap_at_the_published_ceiling_is_not_called_raisable(checks: Checks) -> None:
    cap = ceiling_of(AT_CEILING_MODEL)
    if not cap:
        checks.cannot_pose("a cap at the published ceiling is not called raisable",
                           f"the catalogue has no published max_output_tokens for {AT_CEILING_MODEL}, so "
                           f"there is no ceiling to place a cap AT")
        return
    # A tiny VISIBLE answer at the model's own ceiling — reasoning ate the budget, which is the real shape.
    seed("sig-at-ceiling", AT_CEILING_MODEL, cap, [3] * 40, "max_tokens")
    with contextlib.closing(connection()) as c:
        label, advice, _command = receipt.truncation_remedy(c, AT_CEILING_MODEL)
    checks.that("a cap at the published ceiling is labelled AT CEILING",
                label == receipt.AT_CEILING, f"got {label!r}")
    checks.that("and the advice does NOT promise autotune will raise it",
                "autotune=apply" not in advice, advice)
    checks.that("and it names reasoning as where the budget went", "reasoning" in advice.lower(), advice)


def case_a_provider_prefixed_recording_still_finds_its_cap(checks: Checks) -> None:
    """THE SEAM. `calls` spells a model bare; `gate_calls` may spell it `provider:model`.

    This is the case that made the first version of the fix wrong in the expensive direction. Joining the two
    tables on equality matched NONE of gemini-3.8-flash's 4,191 truncated rows — because they are recorded as
    `gemini:gemini-3.8-flash` — so the largest class on the ledger came back "no observed cap" and was
    labelled RAISABLE, when it is the one class already pinned at its published ceiling. Each side was
    internally correct and the gap between them held the defect, which is why it survived a passing test.
    """
    cap = ceiling_of(AT_CEILING_MODEL)
    if not cap:
        checks.cannot_pose("a provider-prefixed recording still finds its cap",
                           f"the catalogue has no published max_output_tokens for {AT_CEILING_MODEL}")
        return
    # Recorded PREFIXED, exactly as the adapter hands it to the recorder.
    seed("sig-prefixed", f"gemini:{AT_CEILING_MODEL}", cap, [3] * 40, "max_tokens")
    with contextlib.closing(connection()) as c:
        # Asked by the BARE name, exactly as `calls` holds it.
        label, advice, command = receipt.truncation_remedy(c, AT_CEILING_MODEL)
    checks.that("a provider-prefixed recording is found by the bare model name",
                "no observed cap" not in advice, advice)
    checks.that("and it is labelled AT CEILING, not RAISABLE", label == receipt.AT_CEILING, f"got {label!r}")
    checks.that("and a sig was recovered for the command", bool(command), command)


def case_a_cap_below_the_ceiling_is_the_one_autotune_can_fix(checks: Checks) -> None:
    ceiling = ceiling_of(RAISABLE_MODEL)
    if ceiling and BOUNDED_JUDGE_CAP >= ceiling:
        checks.cannot_pose("a cap below the ceiling is the one autotune can fix",
                           f"{RAISABLE_MODEL}'s published ceiling is {ceiling:,}, which is not above the "
                           f"{BOUNDED_JUDGE_CAP}-token bounded-judge cap, so no 'below the ceiling' cap exists")
        return
    seed("sig-raisable", RAISABLE_MODEL, BOUNDED_JUDGE_CAP, [BOUNDED_JUDGE_CAP] * 40, "max_tokens")
    with contextlib.closing(connection()) as c:
        label, advice, _command = receipt.truncation_remedy(c, RAISABLE_MODEL)
    checks.that("a cap below the ceiling is labelled RAISABLE", label == receipt.RAISABLE, f"got {label!r}")
    checks.that("and THIS is where autotune is offered", "autotune=apply" in advice, advice)


def case_uncapped_receipt_has_no_cap_raise(checks: Checks) -> None:
    # max_tokens 0 — autotune's documented contract is that it never adds a cap the caller did not send.
    seed("sig-no-cap", NO_CAP_MODEL, 0, [17] * 40, "length")
    with contextlib.closing(connection()) as c:
        label, advice, _command = receipt.truncation_remedy(c, NO_CAP_MODEL)
    checks.that("a class with no cap is labelled NO CAP SET", label == receipt.NO_CAP, f"got {label!r}")
    checks.that("and the advice says autotune will not add one", "not add one" in advice, advice)


def case_every_command_the_receipt_prints_actually_runs(checks: Checks) -> None:
    """THE LOAD-BEARING ASSERTION. Not "does it look right" — it is EXECUTED.

    A hint is a claim about a command line, and the only way to know such a claim holds is to run it, which is
    exactly what nobody did for `--model`. A usage line and a non-zero exit are how an unrunnable command
    announces itself, so that is what this looks for.
    """
    with contextlib.closing(connection()) as c:
        remedies = {model: receipt.truncation_remedy(c, model)
                    for model in (AT_CEILING_MODEL, RAISABLE_MODEL, NO_CAP_MODEL)}
    for model, (_label, _advice, command) in remedies.items():
        if not checks.that(f"{model}: a command was offered at all", bool(command), "empty command"):
            continue
        checks.that(f"{model}: the command carries no flag (maxtokens takes positionals only)",
                    "--" not in command, command)
        try:
            done = subprocess.run([sys.executable, "-m", "spendguard", *command.split()[1:]],
                                  capture_output=True, text=True, timeout=CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            checks.that(f"{model}: the command returns within {CHILD_TIMEOUT_SECONDS}s", False,
                        "the child interpreter did not exit — reported as a failure, never retried silently")
            continue
        printed = (done.stdout + done.stderr).lower()
        checks.that(f"{model}: the command runs without a usage error",
                    "usage:" not in printed and done.returncode == 0,
                    f"rc={done.returncode} :: {(done.stdout + done.stderr).strip()[:160]}")


def case_the_block_renders_with_a_remedy_for_every_model_it_lists(checks: Checks) -> None:
    """The rows come from `calls`; the caps come from `gate_calls`. A model present in one and absent from the
    other must still render — a remedy that reaches into a table the row may not appear in would make the
    whole tally vanish to report one hint."""
    # `calls.ts` is ISO-8601 TEXT, not an epoch, and the window is compared as a STRING. The first version of
    # this fixture inserted an integer, which SQLite orders before every text value, so the row never matched
    # and the case failed for a reason that had nothing to do with the code under test. The timestamp is
    # derived from the window itself rather than written down, so it cannot drift out of range with the clock.
    inside_the_window = f"{receipt._windows()[2]}T12:00:00+00:00"
    with contextlib.closing(connection()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, ts TEXT, model TEXT, "
                  "finish TEXT, cost REAL)")
        for model in (AT_CEILING_MODEL, RAISABLE_MODEL, NO_CAP_MODEL, "a-model-never-seen-by-the-gate"):
            c.execute("INSERT INTO calls (id, ts, model, finish, cost) VALUES (?,?,?,?,?)",
                      (f"truncation-remedy-fixture-{model}", inside_the_window, model, "max_tokens", 1.0))
        c.commit()
        lines = receipt._truncation_rows(c)
    checks.that("the truncation block rendered", bool(lines), f"got {lines!r}")
    rendered = "\n".join(lines)
    checks.that("and it never emits the flag that does not exist", "--model" not in rendered, rendered[:200])
    checks.that("and a model the gate never recorded does not crash the block",
                all(isinstance(line, str) for line in lines))


def main() -> int:
    """Run every case and report THREE outcomes, never two.

    The cases are named `case_*`, not `test_*`, on purpose: this file is a standalone program and each case
    takes a required `Checks`, so a `pytest tests/` sweep that collected them would error on a missing
    fixture. The enforcer caught that, and renaming removes the hazard rather than papering it with a
    conftest fixture for cases nothing is meant to collect.
    """
    checks = Checks()
    for case in (case_a_cap_at_the_published_ceiling_is_not_called_raisable,
                 case_a_provider_prefixed_recording_still_finds_its_cap,
                 case_a_cap_below_the_ceiling_is_the_one_autotune_can_fix,
                 case_uncapped_receipt_has_no_cap_raise,
                 case_every_command_the_receipt_prints_actually_runs,
                 case_the_block_renders_with_a_remedy_for_every_model_it_lists):
        case(checks)
    # UNPOSED IS NOT A PASS. A case whose precondition the catalogue could not supply asked nothing, so
    # reporting it green would claim coverage this run never had — and that is the one failure a guard
    # against a wrong remediation cannot afford, since it would go quiet exactly when the catalogue drifts.
    verdict = "[FAIL]" if (checks.failures or checks.unposed) else "OK"
    print(f"\n{verdict} test_a_truncation_remedy_names_a_fix_that_runs: "
          f"{checks.failures} failure(s) over {checks.ran} assertion(s); "
          f"{len(checks.unposed)} case(s) could not be posed")
    for unposed in checks.unposed:
        print(f"  UNPOSED {unposed}")
    return 1 if (checks.failures or checks.unposed) else 0


if __name__ == "__main__":
    sys.exit(main())
