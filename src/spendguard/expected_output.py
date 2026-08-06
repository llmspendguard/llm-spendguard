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
  4. nothing — and then we say UNKNOWN, loudly, and never 0.

Order matters: learned beats the published ceiling because a 128k-output model would otherwise inflate every
estimate and trip caps on work costing pennies — the false-refusal failure, arriving from the other side.
"""
import sys

MIN_OBS = 20                 # below this a class's distribution is noise, not a measurement
_warned = set()


def expect(model, sig=None, max_tokens=None):
    """(tokens, basis) for ONE request. `basis` names where the number came from, so a receipt or a block
    message can say it out loud: 'learned' · 'caller-cap' · 'model-max' · 'unknown'."""
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
    if max_tokens:
        return int(max_tokens), "caller-cap"
    try:
        from . import pricing
        lim = pricing.max_output_tokens(model)
    except Exception:
        lim = None
    if lim:
        return int(lim), "model-max"
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
