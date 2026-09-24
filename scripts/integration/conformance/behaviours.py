"""The conformance behaviour MANIFEST — one declarative entry per incident-anchored behaviour (docs/CONFORMANCE.md §3).

DATA ONLY. Each entry declares WHAT the behaviour proves, the incident it guards, its spend_class ('lane' = $0
subscription lanes, 'metered' = requires the paid API), the staged sizes, and a per-task TOKEN MODEL so the zero-spend
estimate (estimate.py) can project the $ BEFORE anything runs. The live setup/run/assert are wired per behaviour in the
runner (built next); this file is the single source the --estimate, --run, and --report paths all read, so the matrix
can't drift from what actually executes.

The `model` fields are CONFIG DEFAULTS (overridable per run) — the exact soak vendors are an open §8 decision and must
NOT be hardcoded into logic; they live here as tunable defaults only. Token models are conservative + reasoning-INCLUSIVE
for reasoning behaviours (est_out counts thinking tokens — the whole point of the reasoning-economics fixes)."""

# Staged sizes (the validation hard-gate: scale only on green). A behaviour may override `stages`.
DEFAULT_STAGES = [100, 250, 500, 1000]

# Representative default models per role — TUNABLE (see §8). Not a hardcode in logic; a config default here.
DEFAULT_REASONING_MODEL = "openai:gpt-5.5"          # exercises the OpenAI reasoning + with_raw_response header path (priced)
DEFAULT_METERED_MODEL = "openai:gpt-5-nano"         # cheap PRICED metered for pacing/failover volume (~$0.0001/call)
DEFAULT_LANE_MODEL = "claude-code:sonnet"           # a $0 subscription lane (never-crash/empty/parking paths; not priced)


def _b(id, title, incident, spend_class, assertion, *, model=None, reasoning=False,
       est_in_tok=400, est_out_tok=120, stages=None, induces=""):
    return dict(id=id, title=title, incident=incident, spend_class=spend_class, assertion=assertion,
                model=model, reasoning=reasoning, est_in_tok=est_in_tok, est_out_tok=est_out_tok,
                stages=stages or DEFAULT_STAGES, induces=induces)


BEHAVIOURS = [
    _b("B1", "fan never CRASHES", "honestreview DispatchTimeout out of a 532-task fan", "lane",
       "0 exceptions; len(results)==N; every row has text OR a typed reason (never a raise)",
       model=DEFAULT_LANE_MODEL, induces="500-task fan on a deliberately-saturated lane (low lane_concurrency)"),
    _b("B2", "fan never EMPTY", "honestreview 766 empty rows from a confined fan", "lane",
       "confined arms empty → widen (loud) or honest no_viable_lane; 0 silent-empty rows",
       model=DEFAULT_LANE_MODEL, induces="confined lanes= all cooling"),
    _b("B3", "no 429 under LOAD", "bc_edges Opus 429-storm at 8 workers", "metered",
       "0 unhandled 429 reaches the caller; admission_state shows waiting>0; provider 429s absorbed/paced",
       model=DEFAULT_METERED_MODEL, est_in_tok=600, est_out_tok=200,
       induces="heavy metered fan at/above a set tpm_<vendor>"),
    _b("B4", "self-calibrate from 429 headers", "Step 3 self-calibration", "metered",
       "after the first 429, learned_limits[vendor].tpm is set; the next burst is 0-429",
       model=DEFAULT_METERED_MODEL, est_in_tok=600, est_out_tok=200, stages=[50, 100, 250],
       induces="unknown-low tpm, then a burst"),
    _b("B5", "lane → METERED failover", "lane cooldowns cascading", "metered",
       "result executor flips lane→api-fallback; the task is still served",
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],   # priced at the metered SHED target
       induces="cool the lane mid-fan (_lane_cool)"),
    _b("B6", "metered → BASE-model failover (tier-3)", "the tier-3 fallback ladder", "metered",
       "the chosen model errors → base_fallback serves; result model == provider base; substituted_from set",
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[25, 50, 100],
       induces="pin a bad served-model id + base_fallback=True"),
    _b("B7", "reasoning NOT cut mid-thought", "the $51.88 gpt-5.5 incident", "metered",
       "deadline_cancels ≈ 0; every task returns a verdict (the reasoning deadline floor holds)",
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[25, 50, 100],
       induces="reasoning fan with the reasoning deadline floor ON"),
    _b("B8", "reasoning-cut DETECTED", "the $51.88 incident (invisible waste)", "metered",
       "note_deadline_cancel increments; the loud invisible-waste line is emitted",
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[10, 25],
       induces="deadline forced BELOW reasoning latency"),
    _b("B9", "estimate ACCURACY", "$33/$175 and $13.69/$51.88 estimate misses", "metered",
       "actual within ±X% of the reasoning-inclusive estimate (X is an open §8 bar; target ≤25%)",
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[50, 100],
       induces="estimate-first a reasoning sample, then run it, compare"),
    _b("B10", "metered_only PINS the model", "warden nano→opus bypass", "metered",
       "panel_providers == the requested set; 0 substitutions",
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],
       induces="bulk_delegate(metered_only=True, model_for=pin)"),
    _b("B11", "max_tokens no silent truncation", "bc_edges silent JSON truncation", "metered",
       "structured reply floored (not empty-as-no-findings); _warn_once_caller_maxtokens fired",
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[10, 25],
       induces="structured schema call with a tiny caller max_tokens"),
    _b("B12", "PARKING under saturation", "Step 4 durable backpressure", "lane",
       "queue_depth.parked > 0 during; drains to done; parks did NOT burn attempts",
       model=DEFAULT_LANE_MODEL, stages=[100, 250, 500],
       induces="saturate the queue, submit + drain"),
    _b("B13", "double-record suppressed", "route_through_queue double-record", "lane",
       "exactly N 'done' rows (not 2N) after a drain; record_route=False held",
       model=DEFAULT_LANE_MODEL, stages=[100, 250, 500],
       induces="drain a fan of N"),
    _b("B14", "metered_only ⇒ NO lane substitution", "warden cross-vendor bandit collapse", "metered",
       "served model == pinned; no cross-vendor collapse (panel_providers)",
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],
       induces="metered_only fan with the bandit denylist unset"),
]

# NAME_REGISTRY note: this is a manifest module; BEHAVIOURS is its one public symbol.
BEHAVIOUR_IDS = [b["id"] for b in BEHAVIOURS]
assert len(BEHAVIOUR_IDS) == len(set(BEHAVIOUR_IDS)), "duplicate behaviour id in the manifest"
