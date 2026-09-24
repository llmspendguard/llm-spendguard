"""The conformance behaviour MANIFEST — one declarative entry per incident-anchored behaviour (docs/CONFORMANCE.md §3).

DATA ONLY. Each entry declares WHAT the behaviour proves, the incident it guards, WHY it is the test (`why` — what a
regression costs in the wild) and the exact surface `assertion` a pass reads, its `spend_class` ('lane' = $0 subscription
lanes, 'metered' = the paid API), a `tier` (run order — see below), the staged sizes, an optional `max_spend_usd`
self-cap, and a per-task TOKEN MODEL so the zero-spend estimate (estimate.py) can project the $ BEFORE anything runs. The
live setup/run/assert are wired per behaviour in the runner; this file is the single source the --estimate, --run and
--report paths all read, so the matrix can't drift from what actually executes.

TIER = hard-stop-aware run order. With a hard $ ceiling, ORDER is a correctness property: if the run stops on budget, the
safety invariants must already have passed. So we run cheapest-and-most-critical first:
  0 — $0 safety rails (free; run them all first, they cost nothing).
  1 — the $ BACKSTOPS (cheap metered, HIGHEST value: prove spendguard actually bounds spend + surfaces the traps).
  2 — pacing & failover (metered).
  3 — reasoning-economics soak (priciest axis — reasoning tokens; run LAST so a budget cut sacrifices only tuning).

The `model` fields are CONFIG DEFAULTS (overridable per run) — the exact soak vendors are an open §8 decision and must
NOT be hardcoded into logic; they live here as tunable defaults only. Token models are conservative + reasoning-INCLUSIVE
for reasoning behaviours (est_out counts thinking tokens — the whole point of the reasoning-economics fixes)."""

# Staged sizes (the validation hard-gate: scale only on green). A behaviour may override `stages`.
DEFAULT_STAGES = [100, 250, 500, 1000]

# Representative default models per role — TUNABLE (see §8). Not a hardcode in logic; a config default here.
DEFAULT_REASONING_MODEL = "openai:gpt-5.5"          # exercises the OpenAI reasoning + with_raw_response header path (priced)
DEFAULT_METERED_MODEL = "openai:gpt-5-nano"         # cheap PRICED metered for pacing/failover volume (~$0.0001/call)
DEFAULT_HONORING_MODEL = "openai:gpt-5-mini"        # a reasoning model whose 'minimal' IS honored (the A control arm)
DEFAULT_LANE_MODEL = "claude-code:sonnet"           # a $0 subscription lane (never-crash/empty/parking paths; not priced)


def _b(id, title, incident, spend_class, assertion, why, *, tier, model=None, reasoning=False,
       est_in_tok=400, est_out_tok=120, stages=None, induces="", max_spend_usd=None):
    return dict(id=id, title=title, incident=incident, spend_class=spend_class, assertion=assertion, why=why,
                tier=tier, model=model, reasoning=reasoning, est_in_tok=est_in_tok, est_out_tok=est_out_tok,
                stages=stages or DEFAULT_STAGES, induces=induces, max_spend_usd=max_spend_usd)


BEHAVIOURS = [
    # ── TIER 0 — $0 SAFETY RAILS (free; run all first) ─────────────────────────────────────────────────────────────
    _b("B1", "fan never CRASHES", "honestreview DispatchTimeout out of a 532-task fan", "lane",
       "0 exceptions; len(results)==N; every row has text OR a typed reason (never a raise)",
       "a fan that raises takes down the CALLER (honestreview crashed) — an uncaught DispatchTimeout is a total-loss "
       "outcome, not a degraded one", tier=0,
       model=DEFAULT_LANE_MODEL, induces="500-task fan on a deliberately-saturated lane (low lane_concurrency)"),
    _b("B2", "fan never EMPTY", "honestreview 766 empty rows from a confined fan", "lane",
       "confined arms empty → widen (loud) or honest no_viable_lane; 0 silent-empty rows",
       "766 silent '' rows read downstream as 'no findings' — a wrong ANSWER with no error, the most dangerous "
       "failure mode there is (looks like success)", tier=0,
       model=DEFAULT_LANE_MODEL, induces="confined lanes= all cooling"),
    _b("B12", "PARKING under saturation", "Step 4 durable backpressure", "lane",
       "queue_depth.parked > 0 during; drains to done; parks did NOT burn attempts",
       "without parking a rate-blocked task either 429s the caller or exhausts its retries and is LOST; parking is how "
       "load is absorbed instead of dropped", tier=0,
       model=DEFAULT_LANE_MODEL, stages=[100, 250, 500], induces="saturate the queue, submit + drain"),
    _b("B13", "double-record suppressed", "route_through_queue double-record", "lane",
       "exactly N 'done' rows (not 2N) after a drain; record_route=False held",
       "a 2x double-record inflates every spend/attribution number the org sees — a governance tool that miscounts its "
       "own work is worse than none", tier=0,
       model=DEFAULT_LANE_MODEL, stages=[100, 250, 500], induces="drain a fan of N"),
    _b("B20", "cold reasoning ESTIMATE uses the floor (guardrail C)", "the $13.69/$51.88 cold-class miss", "lane",
       "expected_output.expect(cold reasoning model, no cap) → basis 'reasoning-floor', value == the reasoning seed, "
       "NOT ~160 / the 128k ceiling / unknown-0; a caller cap still wins",
       "the estimate is the APPROVAL GATE — a cold reasoning class estimated at per_out=160 (~9x under) is exactly how "
       "the $51.88 fan sailed through its gate; this is $0 (pure arithmetic on expect())", tier=0,
       model=DEFAULT_REASONING_MODEL, reasoning=True, stages=[1],
       induces="expect() on a cold reasoning model with and without a caller cap (no live call — estimate arithmetic)"),

    # ── TIER 1 — the $ BACKSTOPS (cheap metered, highest value; prove spendguard actually bounds spend) ─────────────
    _b("B15", "budget_usd HARD-STOPS a live fan (guardrail D)", "the $50.82 #2: estimate sailed through, no running cap",
       "metered",
       "cumulative ACTUAL $ ≤ budget + one chunk; the tail rows are budget_exhausted (unissued, NOT spent-to-N); calls "
       "actually MADE ≈ budget/per-call, not N; no in-flight call was cancelled",
       "this is THE $ backstop and the strongest possible live proof — a real fan told to spend $0.50 out of a job that "
       "would cost far more must STOP at $0.50. If any single behaviour must pass, it is this one", tier=1,
       model=DEFAULT_METERED_MODEL, est_in_tok=400, est_out_tok=120, stages=[200], max_spend_usd=0.75,
       induces="a metered fan of 200 with budget_usd set FAR below the projected total (self-caps the spend)"),
    _b("B18", "spend refusal HALTS, never fail-open", "the deliberate-refusal-never-swallowed doctrine", "metered",
       "the fan STOPS on the typed spend/budget stop (raises OR returns honest refusal rows); it does NOT silently "
       "continue spending; the deliberate-stop TYPE propagated (not swallowed into a generic error row)",
       "a refusal downgraded to 'log and continue' is a spend control that reads as safe and is not — the exact "
       "fail-open class the whole gate exists to prevent; distinct from D (this tests the SEMANTICS, D the accounting)",
       tier=1, model=DEFAULT_METERED_MODEL, est_in_tok=400, est_out_tok=120, stages=[25], max_spend_usd=0.20,
       induces="a fan under a near-zero budget / a forced SpendGateRefused mid-flight"),
    _b("B16", "effort pin HONORED-or-REFUSED (guardrail A)", "the $45.44 gpt-5.5 minimal→none silent drop", "metered",
       "gpt-5.5 + reasoning='minimal' → the wire/recorded effort is the floor 'none' AND unhonored_efforts() records the "
       "non-honor (LOUD); the gpt-5-mini control + 'minimal' is HONORED (wire effort 'minimal', 0 unhonored records)",
       "a 'minimal' pin silently dropped to 'none' on gpt-5.5 burned ~35x the cost (4,249 vs 121 out tok) with no "
       "warning — a control the caller set that silently does nothing; the test proves the drop is now surfaced",
       tier=1, model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=500, est_out_tok=800, stages=[15],
       max_spend_usd=0.60, induces="a small gpt-5.5 fan + a gpt-5-mini control, both reasoning='minimal'"),
    _b("B17", "per-call RUNAWAY recorded (guardrail E)", "the runaway class (out_tok >> the measured norm)", "metered",
       "with a seeded norm + a low runaway_factor, a real reasoning call whose out_tok exceeds factor×p99 is recorded "
       "in runaways(); a normal-length call at the same class does NOT trip",
       "a reasoning runaway sits UNDER the deliberately-loose output ceiling and bills in full × N, invisibly; the "
       "breaker makes it visible so D can cap it and a human can reroute", tier=1,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=500, est_out_tok=900, stages=[20],
       max_spend_usd=0.70, induces="seed a small p99 for the class, then real reasoning calls; low runaway_factor"),
    _b("B19", "effort is PATH-INDEPENDENT (guardrail B)", "the $50.82 governed-path divergence", "metered",
       "the same (model, intent, reasoning) recorded through plain vs governed=True vs bulk_delegate fan reports the "
       "IDENTICAL effort on all three paths",
       "incident #2 ran at a different effort on the governed path than the plain one — the same call must cost the "
       "same whichever door it takes, or a caller cannot reason about spend at all", tier=1,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=400, est_out_tok=700, stages=[9],
       max_spend_usd=0.40, induces="3 small identical fans, one per path, compare recorded effort"),

    # ── TIER 2 — PACING & FAILOVER (metered) ───────────────────────────────────────────────────────────────────────
    _b("B3", "no 429 under LOAD", "bc_edges Opus 429-storm at 8 workers", "metered",
       "0 unhandled 429 reaches the caller; admission_state shows waiting>0; provider 429s absorbed/paced",
       "an unhandled 429 is a task the user LOSES to throttling — the queue exists precisely so the caller never sees "
       "one; this proves the admission governor actually paces real load", tier=2,
       model=DEFAULT_METERED_MODEL, est_in_tok=600, est_out_tok=200,
       induces="heavy metered fan at/above a set tpm_<vendor> (real provider limits per §8)"),
    _b("B4", "self-calibrate from 429 headers", "Step 3 self-calibration", "metered",
       "after the first 429, learned_limits[vendor].tpm is set; the next burst is 0-429",
       "a vendor whose limit is unknown gets paced only AFTER its first storm unless the limit is learned; this proves "
       "one 429 teaches the governor so the next burst is clean", tier=2,
       model=DEFAULT_METERED_MODEL, est_in_tok=600, est_out_tok=200, stages=[50, 100, 250],
       induces="unknown-low tpm, then a burst"),
    _b("B5", "lane → METERED failover", "lane cooldowns cascading", "metered",
       "result executor flips lane→api-fallback; the task is still served",
       "when a $0 lane cools, the atomic pair must fall to that provider's metered API and still ANSWER — a lane miss "
       "that errored instead would drop the task", tier=2,
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],
       induces="cool the lane mid-fan (_lane_cool)"),
    _b("B6", "metered → BASE-model failover (tier-3)", "the tier-3 fallback ladder", "metered",
       "the chosen model errors → base_fallback serves; result model == provider base; substituted_from set",
       "the last hop of lane→metered→base is what turns 'the model id is unavailable' into SOME answer instead of an "
       "error row, when the caller opted into base_fallback", tier=2,
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[25, 50, 100],
       induces="pin a bad served-model id + base_fallback=True"),
    _b("B10", "metered_only PINS the model", "warden nano→opus bypass", "metered",
       "panel_providers == the requested set; 0 substitutions",
       "a cross-vendor panel where the MODEL is the measurement collapses to garbage if nano is silently served as "
       "opus; metered_only must pin every vote to its named vendor", tier=2,
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],
       induces="bulk_delegate(metered_only=True, model_for=pin)"),
    _b("B11", "max_tokens no silent truncation", "bc_edges silent JSON truncation", "metered",
       "structured reply floored (not empty-as-no-findings); _warn_once_caller_maxtokens fired",
       "a structured reply cut at a tiny max_tokens is CORRUPT, not short — parsed as '{}' it reads as 'no findings'; "
       "spendguard must floor the ceiling and teach the caller to stop capping", tier=2,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[10, 25],
       induces="structured schema call with a tiny caller max_tokens"),
    _b("B14", "metered_only ⇒ NO lane substitution", "warden cross-vendor bandit collapse", "metered",
       "served model == pinned; no cross-vendor collapse (panel_providers)",
       "the utilisation bandit picks the model EARLIER than lane routing, so an un-pinned metered_only call still gets "
       "swapped — this proves metered_only ⇒ no_substitution closes that at the root", tier=2,
       model=DEFAULT_METERED_MODEL, est_out_tok=200, stages=[50, 100, 250],
       induces="metered_only fan with the bandit denylist unset"),

    # ── TIER 3 — REASONING-ECONOMICS SOAK (priciest axis; run LAST) ────────────────────────────────────────────────
    _b("B7", "reasoning NOT cut mid-thought", "the $51.88 gpt-5.5 incident", "metered",
       "deadline_cancels ≈ 0; every task returns a verdict (the reasoning deadline floor holds)",
       "a reasoning call cut at the deadline bills its thinking tokens and returns NOTHING — invisible to the local "
       "ledger; the deadline floor keeps a fresh reasoning class from being cut before latency is learned", tier=3,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[25, 50, 100],
       induces="reasoning fan with the reasoning deadline floor ON"),
    _b("B8", "reasoning-cut DETECTED", "the $51.88 incident (invisible waste)", "metered",
       "note_deadline_cancel increments; the loud invisible-waste line is emitted",
       "when a cut DOES happen, it must not be silent — $0 in the local ledger while the provider bills is the "
       "worst-hidden spend there is; the counter makes it reconcilable", tier=3,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[10, 25],
       induces="deadline forced BELOW reasoning latency"),
    _b("B9", "estimate ACCURACY (warm reasoning)", "$33/$175 and $13.69/$51.88 estimate misses", "metered",
       "for a SEEDED reasoning class, the actual out_tok is within ±X% of the reasoning-inclusive estimate (X is an "
       "open §8 bar; target ≤25%) — the measured p90 estimate tracks reality",
       "an estimate that ignores reasoning tokens under-projects a reasoning fan by ~9x and authorises an overspend; "
       "this proves the WARM-class estimate (measured p90) tracks the real reasoning-inclusive cost", tier=3,
       model=DEFAULT_REASONING_MODEL, reasoning=True, est_in_tok=800, est_out_tok=3600, stages=[50, 100],
       induces="estimate-first a seeded reasoning sample, then run it, compare"),
]

# NAME_REGISTRY note: this is a manifest module; BEHAVIOURS is its one public symbol.
BEHAVIOUR_IDS = [b["id"] for b in BEHAVIOURS]
assert len(BEHAVIOUR_IDS) == len(set(BEHAVIOUR_IDS)), "duplicate behaviour id in the manifest"
assert all(b["tier"] in (0, 1, 2, 3) for b in BEHAVIOURS), "every behaviour needs a tier (hard-stop run order)"
