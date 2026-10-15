"""Fail-closed evidence for the subscription-to-metered transition.

Scheduling, free substitute selection and the spend door remain owned by their existing modules.
This module only carries per-call evidence and parses mechanical transport envelopes.
"""
import os
import re
import time
import math
from dataclasses import dataclass, field

from . import config, gate


class LaneRecoveryViolation(gate.SpendGateRefused):
    """A paid transition lacked evidence that the free recovery ladder was exhausted."""


def recovery_setting(key, default):
    value = os.getenv("SPENDGUARD_LANE_" + key.upper(), config._cfg_get("lane_recovery", key, default))
    try:
        value = type(default)(value)
    except (ValueError, TypeError) as error:
        raise LaneRecoveryViolation(f"invalid lane_recovery.{key}: {value}") from error
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise LaneRecoveryViolation(f"invalid lane_recovery.{key}: {value}")
    return value


def lane_failure_shape(result):
    """Parse fixed CLI/HTTP failure shapes; unknown prose is explicitly ambiguous."""
    if not result.get("error") and (result.get("text") or "").strip():
        return "ok"
    status = result.get("status_code") or result.get("http_status")
    if str(status) == "429" or (str(status).isdigit() and 500 <= int(status) <= 599):
        return "transient"
    if result.get("retry_after_s") is not None or result.get("transport_error"):
        return "transient"
    error = str(result.get("error") or "")
    if re.search(r"\b(timeout|timed out|connection reset|ECONNRESET|ECONNREFUSED|not logged in|logged out|"
                 r"logout|auth-event|cold daemon|daemon unavailable|HTTP (?:429|5\d\d))\b", error, re.I):
        return "transient"
    return "ambiguous"


@dataclass
class LaneRecoveryProof:
    lane: str
    classification: str = "ambiguous"
    attempts: int = 0
    retry_limit: int = 0
    shed_checked: bool = False
    queue_checked: bool = False
    gate_authorized: bool = False
    events: list = field(default_factory=list)

    def recovery_event(self, rung, **facts):
        event = {"kind": "lane_recovery", "lane": self.lane, "rung": rung, **facts}
        self.events.append(event)
        gate._log(event)

    def assert_free_exhausted(self):
        if self.retry_limit < 1 or self.attempts < self.retry_limit or not self.shed_checked or not self.queue_checked:
            self.recovery_event("bug", decision="refused", proof=repr(self))
            raise LaneRecoveryViolation("lane→metered without retry/shed/queue proof: " + repr(self))

    def authorize_metered_recovery(self, provider, model, prompt, system, max_tokens):
        self.assert_free_exhausted()
        gate.require()
        # Explicit normal door decision, outside SDK fail-open wrappers. SDK still gates the actual send.
        try:
            gate._rt_precheck(provider, model, max(1, len((system or "") + prompt)), max_tokens)
        except Exception as error:
            self.recovery_event("metered_refused", decision="refused", error=str(error))
            raise
        self.gate_authorized = True
        self.recovery_event("metered_authorized", decision="allowed")

    def assert_metered_recovery(self):
        self.assert_free_exhausted()
        if not self.gate_authorized:
            self.recovery_event("bug", decision="ungated metered")
            raise LaneRecoveryViolation("metered recovery lacks gate authorization")


def retry_free_lane(invoke, proof):
    """Confirm a miss with a bounded configured retry; never kill an invocation in flight."""
    proof.retry_limit = max(1, recovery_setting("attempts", 2))
    result = {}
    for attempt in range(proof.retry_limit):
        if attempt:
            delay = recovery_setting("backoff_s", 1.2) * recovery_setting("backoff_multiplier", 2.0) ** (attempt - 1)
            proof.recovery_event("retry", backoff_s=delay)
            time.sleep(delay)
        result = invoke()
        proof.attempts += 1
        proof.classification = lane_failure_shape(result)
        proof.recovery_event("classified", classification=proof.classification, attempt=proof.attempts)
        if proof.classification == "ok":
            break
    return result
