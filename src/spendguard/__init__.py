"""spendguard — provider-agnostic LLM cost discipline.

A pre-submit cost GATE (overlay on the OpenAI/Anthropic SDKs), a per-run ESTIMATOR,
token-accurate RECONCILERS, and a daily/weekly/monthly spend REPORT — all priced from
one canonical, verifiable price table.

    import spendguard; spendguard.install(cap=75)   # gate every batch in this process
"""
from .pricing import (batch_cost, realtime_cost, estimate, price, normalize,
                      PRICING, PRICING_VERIFIED, PRICING_SOURCE)
from .gate import install, register, SpendGateRefused

__all__ = ["install", "register", "SpendGateRefused", "batch_cost", "realtime_cost",
           "estimate", "price", "normalize", "PRICING", "PRICING_VERIFIED", "PRICING_SOURCE"]
__version__ = "0.1.0"
