# Part 0: grounding before implementation

The honestreview hook sources are outside this checkout. Their documented public entry is
`vendor_call.call` (`src/spendguard/vendor_call.py:398-434`), which admits dispatch, then
`_attempt` calls `adapters.call(..., no_substitution=True)` (`vendor_call.py:555-562`).
Direct hook calls to adapters share the same path. Panel/fan enters `vendor_call.fan_out`
(`vendor_call.py:1228`) or `lane_balance.bulk_delegate` (`lane_balance.py:502`), then adapters.
`scripts/review/panel_review.py:27-30` explicitly requires the gate and selects the pool.

Before this change:

* Classification: structured reset windows at `adapters.py:2521-2531`; confirmed auth via
  `lanes.lane_auth_status` at `2538-2555`; ambiguous suitability uses fallback outcome
  (`337-355`). No general fixed-shape transport classification here: missing wiring.
* Retry: `2465-2477` retries once only with proven-good size history; backoff is a literal
  at `2026`. `codex_exec.py:99-123` separately confirms auth status. Infrastructure exists,
  but first-call transport failures bypass retry: mis-wiring.
* Shed: quality-equivalent free substitutes at `2565-2602`; bypassed by pinned vendor
  calls and `no_metered_fallback`. Ready availability router exists at
  `plan_admission.py:61-78`. Dispatch can shed directly to paid at `adapters.py:1354-1369`
  and `vendor_call.py:426-434`: mis-wiring, with pinning an intentional constraint.
* Hold: `lane_queue.py:806` submit, `852-879` synchronous durable records, `478-493`
  class-aware deferred retries. No hold at the reactive fallback: mis-wiring. Repo-wide
  source/text search found no `resilient_dispatch_queue` module or symbol in this checkout;
  the durable retry facility is lane_queue, not a second queue to build.
* Paid decision: `adapters.py:2620-2624` recurses `_call_once(_skip_lane=True)`.
  SDK gate exists (`gate.py:1062-1135`, guardrail D at `938`), but no ladder proof or
  explicit fail-closed gate call at this transition: mis-wiring.
* Tests: `test_lane_transient_retry.py:33-67` injects sequential recovery;
  `test_lane_auth_transient_robustness.py:72-75` injects auth recovery. Neither exercises
  transient recovery under concurrent hook/panel load. Chaos tier and mutation proof absent.

Line references above describe the original checkout, before edits.
