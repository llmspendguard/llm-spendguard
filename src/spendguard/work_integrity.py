"""Recorded commit assertions over the existing queue and whole-job receipt.

These proofs own no storage, transport, retry, or scheduling mechanism.
"""
from dataclasses import dataclass, field
from typing import Iterable

from . import gate


class WorkIntegrityViolation(gate.SpendGateRefused):
    """A commit would hide a missing or multiply accounted unit of work."""


@dataclass
class WorkIntegrityProof:
    guarantee: str
    events: list = field(default_factory=list)

    def assert_work_partition(self, expected: Iterable[object], dispositions: Iterable[Iterable[object]]) -> None:
        expected = list(map(str, expected))
        dispositions = [list(map(str, group)) for group in dispositions]
        actual = [str(unit) for group in dispositions for unit in group]
        from collections import Counter
        valid = len(set(expected)) == len(expected) and Counter(expected) == Counter(actual)
        event = {"kind": "work_integrity", "guarantee": self.guarantee,
                 "rung": "commit" if valid else "bug", "expected": expected,
                 "dispositions": dispositions,
                 "decision": "allowed" if valid else "refused"}
        self.events.append(event)
        gate._log(event)
        if not valid:
            raise WorkIntegrityViolation("non-partitioned work at " + self.guarantee + ": " + repr(event))
