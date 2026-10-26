"""Smart project tagging — the cascade that decides which project/work a charge belongs to.

  1. deterministic (FREE): repo/cwd/config + kind (meta → 'llm-spendguard'). Covers most rows at zero cost.
  2. corpus context (FREE): the `calls` log's intent/caller, the `conv` frame — refine the deterministic guess.
  3. LLM residual (CAPPED, gated, estimate-first): only the still-ambiguous remainder; a small batched model
     classifies {project}. That cost is spendguard's own → tagged 'llm-spendguard' (kind='meta'). Never auto-run — it spends, so it
     follows the API spend protocol (estimate → confirm → run, with a meta-budget cap). See estimate_llm_retag().
"""

import sys


_EMPTY_PROJECT = "(project_primary IS NULL OR project_primary='')"   # an untagged row, in spend_events terms
_UNRESOLVED_PROJECT = f"({_EMPTY_PROJECT[1:-1]} OR project_primary=?)"
VENV_BACKFILL_SKIP_PREFIX = "[spendguard tag] skipped venv backfill"
VENV_BACKFILL_NO_PRIOR_REASON = "no usable project prior"
VENV_BACKFILL_UNATTRIBUTED_REASON = "prior resolved to unattributed-project sentinel"


def backfill_project_from_venv(led):
    """Fill unresolved project tags from distinct virtual-environment priors without classification spend."""
    from . import budget, conv
    unresolved_venvs = set(led.distinct(
        "venv", filt=f"{_EMPTY_PROJECT} AND venv IS NOT NULL AND venv != ''"))
    unresolved_venvs.update(led.distinct(
        "venv", where={"project_primary": budget.UNATTRIBUTED_PROJECT},
        filt="venv IS NOT NULL AND venv != ''"))
    retagged = 0
    for venv in unresolved_venvs:
        determination = conv.resolve({"venv": venv}, classify_on_miss=False)
        project = (determination or {}).get("project")
        if not project or project == budget.UNATTRIBUTED_PROJECT:
            reason = (VENV_BACKFILL_NO_PRIOR_REASON if not project
                      else VENV_BACKFILL_UNATTRIBUTED_REASON)
            print(f"{VENV_BACKFILL_SKIP_PREFIX}: {venv} ({reason})", file=sys.stderr)
            continue
        retagged += int(led.bulk_retag_project(
            project, filt=f"{_UNRESOLVED_PROJECT} AND venv=?",
            args=(budget.UNATTRIBUTED_PROJECT, venv), actor="tag",
            reason="deterministic: venv prior") or 0)
    return retagged


def retag_deterministic():
    """FREE pass: fill still-unresolved tags in order: meta → venv prior → coarse repo.

    Each pass only touches rows that remain unresolved, so meta precedence is preserved. Returns the number of
    rows changed. Reads/writes spend_events (the money-of-record).
    """
    from . import budget
    led = budget._ledger()
    with budget._lock:
        a = led.bulk_retag_project(
            "llm-spendguard", filt=f"{_UNRESOLVED_PROJECT} AND COALESCE(is_meta,0)=1",
            args=(budget.UNATTRIBUTED_PROJECT,), actor="tag",
            reason="deterministic: meta → llm-spendguard")
        venv_retagged = backfill_project_from_venv(led)
        b = led.bulk_retag_project(
            budget._project(), filt=_UNRESOLVED_PROJECT,
            args=(budget.UNATTRIBUTED_PROJECT,), actor="tag",
            reason="deterministic: unresolved → repo project")
    return venv_retagged + int(a or 0) + int(b or 0)


def move_project(old, new):
    """Re-assign a project tag across the ledger — fixes cwd-fallback mistags (e.g. video-captioning that ran
    from ~/Documents got tagged 'documents' instead of 'vision-pipeline'). Returns rows changed."""
    from . import budget
    old, new = (old or "").strip().lower(), (new or "").strip().lower()
    with budget._lock:
        n = budget._ledger().bulk_retag_project(new, filt="lower(COALESCE(project_primary,''))=?", args=(old,),
                                                actor="tag", reason=f"move: {old} → {new}")
    return int(n or 0)


def cmd(argv=None):
    argv = argv or []
    if len(argv) >= 3 and argv[0] == "move":
        print(f"re-tagged {move_project(argv[1], argv[2])} rows: {argv[1]} → {argv[2]}")
        return 0
    if argv and argv[0] == "estimate":
        print(estimate_llm_retag())
        return 0
    print("usage: spendguard tag move <old-project> <new-project>   |   tag estimate")
    return 1


def ambiguous_count():
    """Rows a human/LLM might still need to disambiguate — untagged after the free pass (should be ~0 once
    deterministic runs, but non-repo or mixed-context charges can remain). Zero-spend."""
    from . import budget, ledger_read
    return ledger_read.read_ledger_count(ledger_read.LedgerReadRequest(
        where_sql=_UNRESOLVED_PROJECT, parameters=(budget.UNATTRIBUTED_PROJECT,)))


def estimate_llm_retag():
    """Zero-spend estimate for the LLM residual pass (per the API spend protocol — estimate BEFORE spending).
    Returns {rows, est_usd, model}. The actual capped pass is a separate, explicitly-approved step."""
    n = ambiguous_count()
    # tiny classifier on a short conversation/intent snippet; pack ~25/req on a nano model. Rough upper bound.
    model = "gpt-5-nano"
    # 6 places, not 4: a small batch's real cost (n=1 → ~$0.00003) must NOT round to $0.00 and read as free —
    # the estimate-first protocol relies on this number being honestly nonzero when there IS spend.
    est_usd = round(n / 25 * 0.0008, 6)   # ~packed reqs × a conservative per-req cost
    return {"rows": n, "est_usd": est_usd, "model": model, "note": "meta cost → billed to project 'llm-spendguard'"}
