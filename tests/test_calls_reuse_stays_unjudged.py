"""Implicit reuse cannot manufacture positive evidence for the advisor or best-value learner."""
import os
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import advise, best_value, calls  # noqa: E402

REUSE_GUARD_PROVIDER = "reuse-guard-provider"
REUSE_GUARD_MODEL = "reuse-guard-model"
REUSE_GUARD_INTENT = "reuse-guard-intent"
REUSE_GUARD_CHAIN = "reuse-guard-chain"
REUSE_GUARD_OUTPUT = "An output reused as evidence for correction does not establish that the output was correct."
REUSE_GUARD_ZERO_COST = 0.0
REUSE_GUARD_JUDGED_CONFIDENCE = calls._CONF["judge"]


def verify_implicit_reuse_preserves_unjudged_learning():
    with patch.object(calls, "call_recording_consent_enabled", return_value=True), \
            patch.object(calls, "_store_prompts", return_value=True):
        prior = calls.record_call(REUSE_GUARD_PROVIDER, REUSE_GUARD_MODEL, "realtime", REUSE_GUARD_ZERO_COST,
                                  output=REUSE_GUARD_OUTPUT, intent=REUSE_GUARD_INTENT, chain=REUSE_GUARD_CHAIN)
        assert prior is not None
        following = calls.record_call(REUSE_GUARD_PROVIDER, REUSE_GUARD_MODEL, "realtime", REUSE_GUARD_ZERO_COST,
                                      prompt=REUSE_GUARD_OUTPUT, intent=REUSE_GUARD_INTENT, chain=REUSE_GUARD_CHAIN)
        assert following is not None
        db = calls._calls_db()
        stored = db.execute("SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (prior,)).fetchone()
        assert stored == (None, None, None), stored
        summary = calls.cost_summary(REUSE_GUARD_INTENT)
        assert len(summary) == 1
        assert summary[0][-2:] == (0, 0), summary
        evidence = advise.evidence(intent=REUSE_GUARD_INTENT)
        arm = evidence[f"{REUSE_GUARD_PROVIDER}:{REUSE_GUARD_MODEL}"]
        assert arm["good"] == 0 and arm["labeled"] == 0, arm
        fingerprint = best_value._evidence_fingerprint(REUSE_GUARD_INTENT)
        assert fingerprint is not None
        calls._link_used(REUSE_GUARD_CHAIN, REUSE_GUARD_OUTPUT)
        assert best_value._evidence_fingerprint(REUSE_GUARD_INTENT) == fingerprint
        for verdict in (True, False):
            calls.feedback(prior, verdict, source="judge")
            measured = db.execute("SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (prior,)).fetchone()
            calls._link_used(REUSE_GUARD_CHAIN, REUSE_GUARD_OUTPUT)
            assert db.execute("SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (prior,)).fetchone() == measured
            measured_arm = advise.evidence(intent=REUSE_GUARD_INTENT)[f"{REUSE_GUARD_PROVIDER}:{REUSE_GUARD_MODEL}"]
            assert measured_arm["labeled"] == REUSE_GUARD_JUDGED_CONFIDENCE
            assert measured_arm["good"] == (REUSE_GUARD_JUDGED_CONFIDENCE if verdict else 0)


verify_implicit_reuse_preserves_unjudged_learning()
print("OK implicit reuse remains unjudged; only real verdicts contribute quality evidence")
