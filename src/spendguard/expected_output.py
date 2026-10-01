"""How many OUTPUT tokens to expect — when the caller no longer tells us.

The estimator used to read `max_tokens` as "expected output". That was always a coupling bug, and it stayed
invisible while everyone set a cap. Then the correct fix for a different problem — *stop capping, because a cap
never controlled cost and a low one destroys the call silently* — made it visible in the worst way:

    WITH max_tokens=8000 : out_tok 8000   est $0.0400
    WITHOUT max_tokens   : out_tok    0   est $0.0000

Output is where the money is (typically 5× the input rate), so an uncapped job estimated most of its cost at
ZERO. An under-estimate is the direction that lets real overspend past a cap unchallenged — the mirror of the
base64 bug: same broken assumption, opposite sign.

`max_tokens` is a blast-radius bound on pathological generation. It is not, and never was, a statement about
how much output to expect. So the estimator stops asking the caller and uses what it has MEASURED:

  1. the call-class's own learned p90 of COMPLETE outputs (bulkgate.maxtokens excludes truncated samples —
     those measure the cap, not the work). p90 is the EXPECTED cost; `recommend` (p99×1.5) is the worst case
     that sizes a termination bound, and they are deliberately different numbers;
  2. the caller's `max_tokens`, if they set one — their own hard bound, and tighter than any published ceiling;
  3. the model's published `max_output_tokens` — and ONLY when it is genuinely an output limit: 961 of 2,572
     upstream entries copy the context window into that field where no ceiling is published, so `pricing`
     rejects a value equal to `max_input_tokens` rather than assume a 1M-token response;
  4. nothing — and then we say UNKNOWN, loudly. THE NUMBER RETURNED IS 0, and the basis is `unknown`.
     This line used to read "and never 0", which was false: a caller doing arithmetic needs a number and
     any invented one would be worse, so 0 is deliberate. But a reader who trusted "never 0" would not
     defend against it, and a docstring that promises a guarantee the code does not keep is worse than no
     docstring — it converts a known hazard into an unknown one. CHECK THE BASIS. A 0 with basis
     `unknown` is an absence of measurement, not a measurement of zero.

Order matters: learned beats the published ceiling because a 128k-output model would otherwise inflate every
estimate and trip caps on work costing pennies — the false-refusal failure, arriving from the other side.
"""
import sys

# Every rung expect() can answer from, measured-first. A reader must be able to tell a MEASUREMENT
# ("learned", "model-history") from a DECLARATION ("declared") from a CEILING ("caller-cap", "model-max") from an
# admission ("unknown"), because a ceiling presented as an expectation over-states a real answer by ~100x.
BASES = ("learned", "declared", "model-history", "caller-cap", "reasoning-floor", "model-max", "unknown")

# The BATCH estimate's basis vocabulary. batch_output_estimate remaps expect()'s rungs ('measured:<intent>' /
# 'measured:model' / 'declared' / 'reasoning-floor' / 'ceiling') for a reader who must tell a MEASUREMENT from a
# DECLARATION from a CEILING at a glance; submit.estimate_jsonl_cost also emits 'measured avg' for a caller-supplied
# avg_out_tokens. 'measured:<intent>' carries the intent, so membership is a predicate, not a flat set — use is_batch_basis().
BATCH_BASES = ("measured:model", "measured avg", "declared", "reasoning-floor", "ceiling")

MIN_OBS = 20                 # below this a class's distribution is noise, not a measurement
_warned = set()


def is_batch_basis(b):
    """True iff `b` is a valid batch_output_estimate basis label — the fixed BATCH_BASES, or a 'measured:<intent>' tag."""
    return bool(b) and (b in BATCH_BASES or str(b).startswith("measured:"))


def expect(model, sig=None, max_tokens=None, declared=None):
    """(tokens, basis) for ONE request. `basis` names where the number came from, so a receipt or a block
    message can say it out loud: 'learned' · 'declared' · 'caller-cap' · 'model-max' · 'unknown'.

    `declared` is a caller's EXPLICIT, intent-specific expected-output figure (per request) — used after the per-class
    LEARNED measurement (the most trustworthy) but BEFORE the broad model-history average and any ceiling, so a caller
    who has measured their output can authorize a job without the ceiling's ~100x over-statement. It is the DECLARED
    half of the batch estimate basis (the measured half is the learned/model-history rungs); see batch_output_estimate."""
    if sig:
        try:
            from . import bulkgate
            b = bulkgate.maxtokens(sig)
            if b and (b.get("n") or 0) >= MIN_OBS and b.get("p90"):
                # p90, NOT the cap-sizing recommendation. `recommend` (p99×1.5) is a WORST case whose job is to
                # size a termination bound; using it as the EXPECTED cost over-states ~4× against a measured
                # p50. Two numbers, two jobs — the same discipline that separated the cap from the estimate,
                # one level down.
                learned = int(b["p90"])
                # A caller's cap still bounds it: they cannot receive more than they allowed.
                return (min(learned, int(max_tokens)) if max_tokens else learned), "learned"
        except Exception:
            pass
    # DECLARED — a caller's explicit, INTENT-SPECIFIC expected output. After the per-class LEARNED measurement (the most
    # trustworthy) but BEFORE the broad model-history average and any ceiling: a declaration the caller is accountable for,
    # for THIS call-class, beats a model-wide average that mixes unrelated classes, and avoids the ceiling's ~100x.
    if declared:
        try:
            return int(declared), "declared"
        except (TypeError, ValueError):
            pass
    # RUNG 2 — this model's measured output across ALL its call-classes. Broader than the class, but still a
    # MEASUREMENT. Below this the only remaining answers are ceilings (the caller's cap, the model's published
    # limit), and a ceiling used as an expectation over-states by ~100x: opus and gpt-5.5 both publish 128,000
    # output tokens, while their real answers here ran 400-2,100. That gap is what made estimate-first unusable
    # for any class without history.
    try:
        from . import bulkgate
        mo = bulkgate.model_outputs(model)
        if mo and (mo.get("n") or 0) >= MIN_OBS and mo.get("p90"):
            broad = int(mo["p90"])
            return (min(broad, int(max_tokens)) if max_tokens else broad), "model-history"
    except Exception:
        pass
    if max_tokens:
        try:
            return int(max_tokens), "caller-cap"
        except (TypeError, ValueError):
            pass          # a non-integer caller cap ('8k', '8000.5') is unusable — fall through to the measured/published rung
    # RUNG 3.5 — a REASONING model with NO caller cap AND NO measured history (class AND model cold). Reached only when
    # the caller named no max_tokens (an explicit cap is a HARD bound and wins above — batch enforces it, so it is the
    # real ceiling there). With no cap, the rungs below mis-estimate a reasoning model badly: its real output is
    # reasoning+answer (thousands of tokens that bill as OUTPUT), so the published 128k ceiling over-counts ~100x and
    # unknown-0 under-counts to nothing. Use the DOCUMENTED reasoning-inclusive floor — the SAME seed bulkgate.maxtokens
    # uses — so a cold reasoning class is estimated reasoning-AWARE, not naive-low (the incident: gpt-5.5 estimated at
    # per_out=160, ~9x under) nor ceiling-high. The measured p90 (RUNG 1/2) replaces it the moment real calls land.
    try:
        from . import models as _mr, bulkgate as _br
        if _mr.reasons_by_default(model):
            return int(_br._reasoning_out_estimate()), "reasoning-floor"
    except Exception:
        pass
    try:
        from . import pricing
        lim = pricing.max_output_tokens(model)
    except Exception:
        lim = None
    if lim:
        return int(lim), "model-max"
    # THE WARNING THIS MODULE PROVIDES FOR THIS EXACT CASE WAS NEVER CALLED. warn_unknown() exists, says
    # "an estimate that silently treats unknown output as ZERO is the bug this module exists to end" — and
    # expect() returned the zero without ever emitting it. Confirmed by two independent validators against
    # the source, found by two vendors: the module that forbids silent zeros was producing one.
    #
    # The 0 stays, because a caller doing arithmetic needs a number and any invented one would be worse.
    # What changes is that it can no longer be silent: the basis says `unknown` AND the warning fires.
    warn_unknown(model)
    return 0, "unknown"


def warn_unknown(model):
    """Say so, once per model. An estimate that silently treats unknown output as ZERO is the bug this module
    exists to end — the reader must know the number is a floor, not a projection."""
    if model in _warned:
        return
    _warned.add(model)
    print(f"[spend_gate] OUTPUT UNKNOWN for '{model}': no max_tokens, no measured history, and no published "
          f"max_output_tokens — the estimate below counts INPUT ONLY and is a FLOOR, not a projection. "
          f"Run the job once to seed the measurement, or `spendguard sync-prices`.", file=sys.stderr)


def batch_output_estimate(model, intent=None, declared_out=None, ceiling=None):
    """Per-request EXPECTED output tokens + an ACCURATE basis label for a BATCH cost estimate — the ESTIMATE (what the
    job will PLAUSIBLY emit), DISTINCT from the ceiling a batch SENDS. This is the fix for the conflation where the batch
    estimate used the model ceiling (~100x over) labelled 'caller-cap', which made a $ cap unusable for authorization (a
    $126 job demanded an $11,823 cap). What we SEND stays the ceiling (billed on ACTUAL tokens); what we ESTIMATE here is
    what the job will plausibly emit.

    Priority — reusing expect()'s measured ladder (NO duplication): MEASURED (this intent's learned p90 → the model's
    measured p90) → DECLARED (the caller's expected_out_tokens) → the CEILING, named honestly. Returns (per_request_out,
    basis) with basis ∈ 'measured:<intent>' | 'measured:model' | 'declared' | 'reasoning-floor' | 'ceiling'. `ceiling` is
    the per-request SEND ceiling (adapters.output_budget) — used ONLY as the last-resort estimate, never the primary, and
    the separate worst-case ceiling guard still backs it."""
    sig = None
    if intent:
        try:
            from . import bulkgate
            sig = bulkgate.sig(model, template_id=intent)
        except Exception:
            sig = None
    tok, basis = expect(model, sig=sig, max_tokens=None, declared=declared_out)
    label = {"learned": ("measured:" + str(intent)) if intent else "measured:class",
             "model-history": "measured:model", "declared": "declared",
             "reasoning-floor": "reasoning-floor"}.get(basis)
    if label and tok and int(tok) > 0:
        # The estimate can't exceed the max_tokens that will actually be SENT: for Anthropic that is the large send
        # CEILING (so this is a no-op), but for an OpenAI jsonl line it is the CALLER's own max_tokens — a real bound the
        # output cannot exceed — so a measured/declared/reasoning figure above it is capped to it and named 'ceiling'.
        if ceiling and int(tok) > int(ceiling):
            return int(ceiling), "ceiling"
        return int(tok), label
    # expect() reached a CEILING rung (model-max) or 'unknown' (0): the cold fallback is the SEND ceiling, named honestly.
    # A caller avoids it by declaring expected_out_tokens or by seeding one measured run; the worst-case guard backs it.
    c = int(ceiling) if ceiling else int(tok or 0)
    return c, "ceiling"
