"""The lane-routing seams make $0-only literal and prefer free capacity before priced models."""
import spendguard

spendguard.require()

from spendguard.generate.execution import adapters  # noqa: E402
from spendguard.manage.admission.spend_refusal_error import MeteredCallRefused  # noqa: E402
from spendguard.manage.routing import best_value  # noqa: E402
import spendguard.manage.admission.admission_accessors as admission_accessors  # noqa: E402

LANE_PIN_MODEL = "openai:gpt-5.6-sol"
LANE_PIN_PROMPT = "prove the seam"
LANE_PIN_MAX_TOKENS = 64
AMBIENT_METERED_EXECUTOR = "api"
BEST_VALUE_INTENT = "lane-seam-regression"
BEST_VALUE_EXPECTED_LANE = "zai-coding"
BEST_VALUE_EXPECTED_MODEL = "zai:glm-5.3"


lane_name = adapters._LANES["openai"][0]
original_executor = adapters._executor
original_api_key = adapters.config.api_key
adapters._executor = lambda: AMBIENT_METERED_EXECUTOR
adapters.config.api_key = lambda _name: (_ for _ in ()).throw(AssertionError("metered seam was reached"))

try:
    try:
        result = adapters._call_once(
            LANE_PIN_MODEL, LANE_PIN_PROMPT, max_tokens=LANE_PIN_MAX_TOKENS,
            no_metered_fallback=True, _no_sub=True,
        )
    except MeteredCallRefused as refusal:
        assert lane_name in str(refusal)
        assert "ambient-api" in str(refusal)
    else:
        assert result["executor"] == lane_name
        assert result["served_by_metered_api"] is False
        assert result["cost"] == 0.0
finally:
    adapters._executor = original_executor
    adapters.config.api_key = original_api_key

# Mutation proof: deleting the final seam refusal lets this path reach config.api_key; the assertion above REDS.


original_ready_lane = admission_accessors.resolve__best_ready_alt_lane
original_effort_for = best_value.models.effort_for
original_recommend_models = best_value.advisor.recommend_models
admission_accessors.resolve__best_ready_alt_lane = (
    lambda primary, tier: (BEST_VALUE_EXPECTED_LANE, BEST_VALUE_EXPECTED_MODEL)
)
best_value.models.effort_for = lambda model, intent: None
best_value.advisor.recommend_models = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError("priced advisor ran before an eligible $0 lane"))

try:
    result = best_value.select_model_effort(
        BEST_VALUE_INTENT, requested_model=None, pin_model=False,
    )
    assert result["model"] == BEST_VALUE_EXPECTED_MODEL
    assert result["considered"]["source"] == "eligible-free-lane"
    assert BEST_VALUE_EXPECTED_LANE in result["why"]
finally:
    admission_accessors.resolve__best_ready_alt_lane = original_ready_lane
    best_value.models.effort_for = original_effort_for
    best_value.advisor.recommend_models = original_recommend_models

# Mutation proof: deleting the free-lane branch calls recommend_models; the assertion above REDS.

print("OK test_lane_pin_metered_leak_refuses_or_rides_lane")
