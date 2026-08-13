"""One store for "a model ruled on this, and here is what it said."

WHY IT EXISTS. Two guards in this package follow the same shape — find every site mechanically, have a
model rule on each, record the verdict, and fail on any site with none: `token_caps` (is this output cap a
harmless probe or a real content call?) and `estimate_literals` (is this literal-fed cost call a quoted
price or a priceability probe?). Written independently, they grew their own `key`, `ledger_path` and
`compare_to_ledger`, and the name-uniqueness guard caught all three the moment the second one landed.

Three identical helpers under three shared names is the collision this repo has been paying for elsewhere:
a reader who fixes a bug in one copy has not fixed it in the other, and nothing says so. So the machinery
moved here and the two modules keep only what actually differs — how a site is found, what question the
model is asked, and which verdict values are a failure.

THE ONE INVARIANT, HELD HERE. A site with no recorded verdict is a FAILURE, never a pass. That is the whole
reason a ledger exists rather than a lint rule: to reintroduce one of these defects you would have to add
the site AND get a model to certify it AND commit the certificate. It cannot happen by forgetting.
"""
import json
import pathlib


def verdict_path(repo_root, ledger_name):
    """Where a ledger lives. In tests/ on purpose — it is committed evidence, reviewed in the diff."""
    return pathlib.Path(repo_root) / "tests" / ledger_name


def load_verdicts(repo_root, ledger_name):
    p = verdict_path(repo_root, ledger_name)
    return json.loads(p.read_text() or "{}") if p.exists() else {}


def save_verdicts(repo_root, ledger_name, verdicts):
    """Sorted + atomic + backed up. A certificate replaced by a half-written file would silently
    un-certify everything it covered, so this goes through config.update_json like every other whole-file
    JSON write in this package."""
    from . import config
    config.update_json(str(verdict_path(repo_root, ledger_name)),
                       lambda _d: dict(sorted(verdicts.items())))


def compare_to_verdicts(present, verdicts, failing):
    """Sites that exist vs verdicts on record.

    `present` is {key: site}; `failing` names the verdict values that constitute a defect for this subject.
    Returns unjudged / failed / cleared / stale. `unjudged` and `failed` are both test failures — the first
    because nobody looked, the second because someone looked and it was bad."""
    return {
        "total": len(present),
        "unjudged": [s for k, s in present.items() if k not in verdicts],
        "failed": [{**present[k], **verdicts[k]} for k in present
                   if k in verdicts and verdicts[k].get("verdict") in failing],
        "cleared": [k for k in present
                    if k in verdicts and verdicts[k].get("verdict") not in failing],
        "stale": [k for k in verdicts if k not in present],
    }
