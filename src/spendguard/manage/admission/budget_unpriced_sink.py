"""Sink bridge so a cyclic capture adapter can record an UNPRICED spend event without importing budget.

A direct-SDK capture adapter (generate.spend_capture.bedrock_adapter) meters a streamed Bedrock call whose token
usage it cannot read, and records it as UNPRICED so `spendguard receipt` can still name the spend. Importing
budget to call budget.record_unpriced re-created the module import cycle; this dependency-free leaf lets the
adapter record through an injected sink instead. budget wires its real record_unpriced here at its eager import.
"""

_unpriced_recorder = None


def set_unpriced_recorder(recorder):
    """Injected by budget with its live record_unpriced at budget's eager import."""
    global _unpriced_recorder
    _unpriced_recorder = recorder


def record_unpriced_event(provider, model, kind, in_tok=0, out_tok=0, project=None):
    """Record an unpriced spend event through budget's injected recorder; fail LOUD if the sink is unset."""
    if _unpriced_recorder is None:
        raise RuntimeError("import spendguard before recording an unpriced event so budget can wire its sink")
    return _unpriced_recorder(provider, model, kind, in_tok=in_tok, out_tok=out_tok, project=project)
