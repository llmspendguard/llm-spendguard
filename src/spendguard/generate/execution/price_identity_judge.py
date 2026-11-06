"""Agentic model-identity resolution and third-party price-gap auditing."""
import json
import re

from ...store.reference_data import pricing


_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

_SYS_SAME_MODEL = (
    "You match model identifiers across two catalogs. Two ids name the SAME model only if they are the "
    "same weights from the same vendor at the same version — a dated snapshot and its floating alias are "
    "the same model. A different size, version, vendor, or modality is NOT the same model, however similar "
    "the names look. If no candidate is the same model, say so; a wrong match is worse than no match."
)


def _same_model_as_ours(ours, their_ids, run=False, advisor=None, fact_key="openrouter_id"):
    """Return our ids mapped to ids that agentically resolve to the same model."""
    from . import adapters
    from ...interface import ui
    from ...store.call_corpus import calls
    from ...store.configuration import config
    from ...store.reference_data import models as _models

    theirs = set(their_ids)
    out, unresolved = {}, []
    for model_id in ours:
        if model_id in theirs:
            out[model_id] = model_id
            continue
        rec = _models.facts(model_id).get(fact_key)
        if rec is not None:
            if rec[0]:
                out[model_id] = rec[0]
            continue
        unresolved.append(model_id)
    stats = {"exact": len(out), "agentic": 0, "unresolved": len(unresolved)}
    if not unresolved:
        return out, stats

    model = advisor or config.advisor_model()
    prompt = (
        "Our catalog ids:\n"
        + "\n".join(f"  {model_id}" for model_id in unresolved)
        + "\n\nCandidate ids from the other catalog:\n"
        + "\n".join(f"  {candidate_id}" for candidate_id in sorted(theirs))
        + "\n\nFor each of OUR ids, give the candidate that names the same model, or null.\n"
        + 'Reply JSON only: {"matches": [{"ours": "<id>", "theirs": "<id or null>"}]}'
    )
    if not run:
        ui.estimate_only(
            action=f"resolve {len(unresolved)} model ids against the other catalog",
            cost=pricing.realtime_cost(
                model,
                max(1, len(_SYS_SAME_MODEL + prompt) // 4),
                30 * len(unresolved),
            ),
        )
        return out, stats
    with calls.context(intent="spendguard:resolve-model-identity"):
        response = adapters.call(
            model,
            prompt,
            max_tokens=30 * len(unresolved) + 400,
            system=_SYS_SAME_MODEL,
        )
    if response.get("error"):
        return out, stats
    try:
        blob = re.search(r"\{.*\}", response.get("text") or "", re.S)
        matches = (json.loads(blob.group(0)).get("matches") if blob else []) or []
    except Exception:
        matches = []
    unresolved_ids = set(unresolved)
    for item in matches:
        ours_id, theirs_id = (item or {}).get("ours"), (item or {}).get("theirs")
        if ours_id not in unresolved_ids:
            continue
        if theirs_id and theirs_id not in theirs:
            continue
        _models.add_fact(
            ours_id,
            fact_key,
            theirs_id or "",
            confidence=0.9,
            source=f"agentic id-resolution ({fact_key}, {model})",
            verified=False,
        )
        if theirs_id:
            out[ours_id] = theirs_id
            stats["agentic"] += 1
    stats["unresolved"] = len(unresolved) - stats["agentic"]
    return out, stats


def cross_check_openrouter(run=False, scope=None):
    """Price cross-check against OpenRouter's public models JSON. (rows, matched, total)."""
    import json as _json
    import urllib.request

    from ...store.configuration import config
    from ...store.reference_data import model_catalog

    request = urllib.request.Request(_OPENROUTER_URL, headers={"User-Agent": "spendguard/0.1"})
    data = _json.loads(
        urllib.request.urlopen(request, context=config.ssl_context(), timeout=10).read()
    )
    openrouter_prices = {}
    for model_row in data.get("data", []):
        price_row = model_row.get("pricing") or {}
        try:
            openrouter_prices[model_row["id"]] = (
                float(price_row.get("prompt", 0)) * pricing.ONE_MILLION,
                float(price_row.get("completion", 0)) * pricing.ONE_MILLION,
            )
        except Exception:
            pass
    exact = {
        model_id: model_id
        for model_id in pricing.PRICING
        if model_id in openrouter_prices
    }
    ours = list(scope) if scope is not None else list(model_catalog.all_records())
    todo = [model_id for model_id in ours if model_id not in exact]
    resolved, id_stats = _same_model_as_ours(todo, openrouter_prices.keys(), run=run)
    pairs = {**exact, **resolved}
    id_stats["exact"] = len(exact)
    rows, differs = [], []
    for model_id, their_id in sorted(pairs.items()):
        price_row = pricing.PRICING.get(model_id)
        if not price_row:
            continue
        their_input, their_output = openrouter_prices[their_id]
        if (their_input, their_output) == (price_row["in_"], price_row["out"]):
            rows.append(
                (
                    model_id,
                    price_row["in_"],
                    their_input,
                    price_row["out"],
                    their_output,
                    "identical",
                )
            )
        else:
            differs.append(
                (model_id, price_row["in_"], their_input, price_row["out"], their_output)
            )
    if differs:
        rows.extend(_judge_price_gaps(differs, run=run))
    return rows, len(rows), len(pricing.PRICING)


_SYS_PRICE_GAP = (
    "You audit a pricing table against a third-party catalog. For each row you are given OUR published "
    "rate and THEIRS, in USD per 1M tokens. Decide whether the gap means OUR rate is likely WRONG or STALE "
    "and should be re-verified against the vendor's own page. A gap is NOT evidence our rate is wrong when "
    "the other catalog is plausibly quoting a reseller markup, a different modality or context tier, or a "
    "stale listing. Judge each row on the size of the gap RELATIVE TO THE MONEY IT MOVES, not on a fixed "
    "percentage."
)


def _judge_price_gaps(differs, run=False, advisor=None):
    """Return price-gap rows with an agentic recheck verdict."""
    from . import adapters
    from ...interface import ui
    from ...store.call_corpus import calls
    from ...store.configuration import config

    model = advisor or config.advisor_model()
    listing = "\n".join(
        f"  {index}. {model_id}: ours in ${our_input}/out ${our_output} | "
        f"theirs in ${their_input}/out ${their_output}"
        for index, (model_id, our_input, their_input, our_output, their_output) in enumerate(differs)
    )
    prompt = (
        listing
        + '\n\nReply JSON only: {"rows": [{"i": <index>, "recheck": true|false, '
        '"why": "<short>"}]}'
    )
    if not run:
        ui.estimate_only(
            action=f"judge {len(differs)} price gaps against the other catalog",
            cost=pricing.realtime_cost(
                model,
                max(1, len(_SYS_PRICE_GAP + prompt) // 4),
                40 * len(differs),
            ),
        )
        return [(*row, "unjudged (estimate only)") for row in differs]
    with calls.context(intent="spendguard:judge-price-drift"):
        response = adapters.call(
            model,
            prompt,
            max_tokens=40 * len(differs) + 400,
            system=_SYS_PRICE_GAP,
        )
    verdicts = {}
    if not response.get("error"):
        try:
            blob = re.search(r"\{.*\}", response.get("text") or "", re.S)
            for item in (json.loads(blob.group(0)).get("rows") if blob else []) or []:
                verdicts[int(item["i"])] = (
                    "RECHECK" if item.get("recheck") else "ok",
                    (item.get("why") or "")[:60],
                )
        except Exception:
            verdicts = {}
    out = []
    for index, row in enumerate(differs):
        verdict, why = verdicts.get(index, ("undecided", "judge did not answer"))
        out.append((*row, f"{verdict} — {why}" if why else verdict))
    return out
