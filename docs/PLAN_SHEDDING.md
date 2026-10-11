# Plan admission and shedding

Plan admission routes labelled, substitutable work away from subscription lanes that should not receive more discretionary load. It is a zero-spend routing decision over structured lane state; it does not decide semantic equivalence on the hot path ([`src/spendguard/plan_admission.py:78`](../src/spendguard/plan_admission.py#L78)).

## Risk inputs

`lane_risk()` marks a lane at risk when any of these inputs is true:

- its known `remaining_pct` is at or below `plan_admission.remaining_pct`;
- its subagent-host plan is currently on paid overage;
- its name appears in the comma-separated `advisor.shed_lanes` setting.

Unknown quota is not treated as exhausted. The result includes the observed remaining percentage, whether that value is known, the individual capped/overage/proactive flags, and test-isolation provenance ([`src/spendguard/plan_admission.py:7`](../src/spendguard/plan_admission.py#L7), [`src/spendguard/plan_admission.py:20`](../src/spendguard/plan_admission.py#L20)).

`advisor.shed_lanes` is provider-agnostic proactive quota preservation. It lets an operator keep scarce capacity for work that only that lane can do even before the plan is capped. It is a routing preference, not proof that the lane is unusable ([`src/spendguard/plan_admission.py:11`](../src/spendguard/plan_admission.py#L11)).

## Ready lanes and ranking

`_ready(lane)` requires the lane to exist, be enabled, have an authentication state of either `ok` or `unknown`, and not itself be at risk. Authentication is deliberately optimistic: `unknown` commonly means a CLI has no cheap status probe, not that the user is logged out. Confirmed-down, missing, disabled, or at-risk lanes are excluded ([`src/spendguard/plan_admission.py:39`](../src/spendguard/plan_admission.py#L39)).

Alternatives use this order:

1. Confirmed semantic substitutes from the intent's existing substitute registry.
2. If `plan_admission.availability_fallback` is enabled (the default), a model on the best ready alternative lane even when no substitute has been quality-confirmed.

Within either tier, `route_utility.rank_lanes()` prefers available lanes by usable plan capacity, pace, and reset urgency; unknown-headroom lanes remain candidates behind scored ready lanes. Cooling, protected-ahead-of-pace, and capped lanes are unavailable ([`src/spendguard/plan_admission.py:92`](../src/spendguard/plan_admission.py#L92), [`src/spendguard/plan_admission.py:112`](../src/spendguard/plan_admission.py#L112), [`src/spendguard/route_utility.py:95`](../src/spendguard/route_utility.py#L95)). Setting `plan_admission.availability_fallback=false` restores the proven-substitute-only posture.

## Keep, redirect, and refuse

When the primary lane is healthy, admission keeps the requested model. When it is at risk and a ready alternative exists, admission redirects and records the requested model, chosen lane, and reason in call context ([`src/spendguard/plan_admission.py:78`](../src/spendguard/plan_admission.py#L78), [`src/spendguard/adapters.py:1240`](../src/spendguard/adapters.py#L1240)).

If no ready alternative exists, behavior depends on why the primary was at risk:

- **Proactive shedding only:** keep the primary. Preserving quota is a preference and must not turn an otherwise serviceable call into a refusal.
- **Actually capped or on paid overage:** refuse with `PlanAdmissionRefused`; do not silently fail open onto an exhausted or billing lane.

This distinction is implemented at the final admission branch ([`src/spendguard/plan_admission.py:126`](../src/spendguard/plan_admission.py#L126)) and surfaced by `adapters.call()` as a typed result ([`src/spendguard/adapters.py:1194`](../src/spendguard/adapters.py#L1194)). Internal cheap meta-model selection is softer: `ready_meta_model()` selects the best ready lane when possible but retains its default model if none is available, leaving the normal call path to handle it ([`src/spendguard/plan_admission.py:136`](../src/spendguard/plan_admission.py#L136)).

## Exclusions: pinned and measurement calls

Cross-provider shedding applies only to labelled calls whose model is substitutable. `adapters.call()` invokes plan admission only when the call has an intent/signature, the caller did not pin the model, the call is not a probe, and it has no image input ([`src/spendguard/adapters.py:1194`](../src/spendguard/adapters.py#L1194)).

Pinned calls preserve the caller's executor contract. Measurement calls are also provider/model-locked: `measurement=True` and `metered_only=True` each make the call non-substitutable, preventing best-value, lane-bandit, or plan-admission swaps from changing what is measured ([`src/spendguard/adapters.py:1000`](../src/spendguard/adapters.py#L1000), [`src/spendguard/adapters.py:1049`](../src/spendguard/adapters.py#L1049)). Deterministic measurements with generation knobs are additionally excluded from storm coalescing because batching or dropping those knobs would change the experimental conditions ([`src/spendguard/adapters.py:1286`](../src/spendguard/adapters.py#L1286)).
