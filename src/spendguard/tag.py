"""Smart project tagging — the cascade that decides which project/work a charge belongs to.

  1. deterministic (FREE): repo/cwd/config + kind (meta → 'llmseg'). Covers most rows at zero cost.
  2. corpus context (FREE): the `calls` log's intent/caller, the `conv` frame — refine the deterministic guess.
  3. LLM residual (CAPPED, gated, estimate-first): only the still-ambiguous remainder; a small batched model
     classifies {project}. That cost is spendguard's own → tagged 'llmseg'. Never auto-run — it spends, so it
     follows the API spend protocol (estimate → confirm → run, with a meta-budget cap). See estimate_llm_retag().
"""


def retag_deterministic():
    """FREE pass: fill EMPTY project tags from context (meta → 'llmseg', else the repo/config project). Never
    overrides an existing tag. Returns the number of rows changed."""
    from . import budget
    proj = budget._project()
    db = budget._db()
    with budget._lock:
        a = db.execute("UPDATE charges SET project='llmseg' WHERE (project IS NULL OR project='') AND kind='meta'").rowcount
        b = db.execute("UPDATE charges SET project=? WHERE (project IS NULL OR project='')", (proj,)).rowcount
        db.commit()
    return int(a or 0) + int(b or 0)


def move_project(old, new):
    """Re-assign a project tag across the local ledger — fixes cwd-fallback mistags (e.g. video-captioning that
    ran from ~/Documents got tagged 'documents' instead of 'vision-pipeline'). Returns rows changed."""
    from . import budget
    old, new = (old or "").strip().lower(), (new or "").strip().lower()
    db = budget._db()
    with budget._lock:
        n = db.execute("UPDATE charges SET project=? WHERE lower(COALESCE(project,''))=?", (new, old)).rowcount
        db.commit()
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
    from . import budget
    db = budget._db()
    with budget._lock:
        r = db.execute("SELECT COUNT(*) FROM charges WHERE project IS NULL OR project=''").fetchone()
    return int(r[0] or 0)


def estimate_llm_retag():
    """Zero-spend estimate for the LLM residual pass (per the API spend protocol — estimate BEFORE spending).
    Returns {rows, est_usd, model}. The actual capped pass is a separate, explicitly-approved step."""
    n = ambiguous_count()
    # tiny classifier on a short conversation/intent snippet; pack ~25/req on a nano model. Rough upper bound.
    model = "gpt-5-nano"
    est_usd = round(n / 25 * 0.0008, 4)   # ~packed reqs × a conservative per-req cost
    return {"rows": n, "est_usd": est_usd, "model": model, "note": "meta cost → billed to project 'llmseg'"}
