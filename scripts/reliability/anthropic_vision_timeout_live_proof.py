#!/usr/bin/env python3
"""LIVE end-to-end proof for a34ef34 — an explicit `timeout_s` no longer breaks the Anthropic VISION path.

The offline guards (tests/test_anthropic_vision_timeout.py + tests/test_anthropic_vision_hang_bounded.py) prove the
BEHAVIOUR with a faked SDK: a vision call built with `timeout_s` OMITS the httpx client timeout that used to surface as
a spurious "Connection error." → None, keeps the daemon-join wall-clock bound, and stays bounded when the transport
hangs. The ONE thing no offline test can exercise is the layer they fake: that the REAL Anthropic SDK, handed a real
image body under a real `timeout_s`, actually returns a caption instead of collapsing — which is the exact failure
a34ef34 fixed (a bakeoff slate of claude-opus-4-8 failed 40/40 on 7thsense-vision-caption before the fix). This script
proves that layer, live, with ONE metered call:

    adapters.call(<model>, <prompt>, images=[<generated probe PNG>], timeout_s=<t>)  → a non-empty caption, error=None

Estimate-first, per the spend protocol: with NO --live it does a SEPARATE zero-spend estimate (image tokens via the
real per-image rule + text + the output ceiling, priced by pricing.realtime_cost) and REFUSES if it exceeds --cap. Only
--live makes the real call (fractions of a cent at the default cap). Nothing hardcoded: --model / --cap / --out-cap /
--timeout-s / --intent are inputs; the probe image is GENERATED (a small 2-tone RGB PNG), not a magic base64 blob.

Run under the gated venv (spendguard doctor == ENFORCING HERE: YES).

Usage:
    # phase 1 — the SEPARATE zero-spend estimate (default; no spend):
    .venv.nosync/bin/python scripts/reliability/anthropic_vision_timeout_live_proof.py
    # phase 2 — the ONE real vision call (cents' fraction, capped):
    .venv.nosync/bin/python scripts/reliability/anthropic_vision_timeout_live_proof.py --live
"""
import argparse
import base64
import struct
import sys
import zlib

# --- named constants (the proof's own params + a tiny blast-radius bound; every one overridable per run) ---
DEFAULT_VISION_MODEL = "anthropic:claude-opus-4-8"   # the model the a34ef34 trap was MEASURED on (40/40 fail → fixed); --model overrides
DEFAULT_PROOF_CAP_USD = 0.25          # the estimate must come in under this or the call is REFUSED (a tiny bound; --cap overrides)
DEFAULT_OUT_CAP_TOKENS = 128          # the output ceiling for the caption (also the worst-case the estimate prices)
DEFAULT_TIMEOUT_S = 120               # the SAME timeout_s the trap needed — the value under test (a34ef34: safe on vision now)
DEFAULT_INTENT = "anthropic-vision-timeout-live-proof"
_PROBE_PROMPT = "In one short sentence, describe this image."
_PROBE_PNG_SIZE = 16                  # a 16x16 probe: two colour bands, so a REAL caption has something to say


def _make_probe_png_data_url(size=_PROBE_PNG_SIZE):
    """A small, real, captionable RGB PNG — top half red, bottom half blue — GENERATED with stdlib (no magic blob), so
    the live model returns a substantive caption and _load_image reads real dimensions (→ real image-token pricing)."""
    rows = []
    for y in range(size):
        r, g, b = (200, 30, 30) if y < size // 2 else (30, 30, 200)
        rows.append(b"\x00" + bytes([r, g, b]) * size)      # PNG filter byte 0 (None) + one row of RGB pixels
    raw = b"".join(rows)

    def _chunk(typ, data):
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))   # 8-bit, colour-type 2 (RGB)
           + _chunk(b"IDAT", zlib.compress(raw))
           + _chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _estimate_cost(model, images, out_cap):
    """Worst-case $ for the one call: image tokens via the REAL per-image rule + text (chars/4) input, OUTPUT at the
    ceiling (the most it can bill). Returns (in_tok, out_tok, cost, provider). Over-estimate by design — safe to gate on."""
    from spendguard import adapters, pricing
    prov = adapters.provider_for(model)
    raw = model.split(":", 1)[1] if ":" in model else model
    loaded = [adapters._load_image(img) for img in images]
    img_tok = adapters._image_input_tokens(loaded, prov, raw)
    txt_tok = max(1, len(_PROBE_PROMPT) // 4)
    in_tok, out_tok = txt_tok + img_tok, out_cap
    cost = pricing.realtime_cost(raw, in_tok, out_tok, provider=prov)
    return in_tok, out_tok, cost, prov


def estimate_only(model, cap, out_cap):
    """Phase 1 — the SEPARATE zero-spend estimate. Prints the worst-case cost and whether it clears --cap."""
    import json
    images = [_make_probe_png_data_url()]
    in_tok, out_tok, cost, prov = _estimate_cost(model, images, out_cap)
    under = cost is not None and cost <= cap
    print(json.dumps({"phase": "estimate", "model": model, "provider": prov, "cap_usd": cap,
                      "in_tok_incl_image": in_tok, "out_tok_ceiling": out_tok,
                      "est_cost_usd_worstcase": (round(cost, 6) if cost is not None else None),
                      "under_cap": under,
                      "note": "output priced at the %d-tok ceiling; a one-sentence caption emits far fewer, so the "
                              "ACTUAL cost is a fraction of this. Run again with --live to make the ONE real call."
                              % out_cap}, indent=2))
    return 0 if under else 3


def run_live(model, cap, out_cap, timeout_s, intent):
    """Phase 2 — the ONE real metered vision call. Estimates first and REFUSES over --cap (belt-and-suspenders with the
    gate), then calls adapters.call(images=…, timeout_s=…) and asserts a real caption came back (error=None, non-empty
    text, metered executor). This is the property no offline test can prove: the real SDK + a real image + timeout_s."""
    import json
    from spendguard import adapters
    images = [_make_probe_png_data_url()]
    in_tok, out_tok, est, prov = _estimate_cost(model, images, out_cap)
    if est is None or est > cap:
        print("REFUSED: estimate $%s exceeds cap $%s (raise --cap deliberately to proceed)"
              % (("unknown" if est is None else round(est, 6)), cap), file=sys.stderr)
        return 3
    # PIN the vendor with no_substitution=True: the MODEL is the measurement here (the Anthropic SDK + a real image +
    # timeout_s is exactly what a34ef34 fixed), so without the pin the lane bandit swaps in another vendor (measured:
    # gemini-3.8-flash) and the "proof" exercises the WRONG SDK. max_tokens is intentionally omitted — spendguard owns
    # the output budget and bills on ACTUAL tokens (a caption emits ~30); the estimate above still prices the ceiling.
    r = adapters.call(model, _PROBE_PROMPT, images=images, timeout_s=timeout_s,
                      intent=intent, sig=intent, no_substitution=True)
    text = (r.get("text") or "").strip()
    served = adapters.served_by(r)                          # who ACTUALLY answered — never the requested label
    substituted = adapters.was_substituted(r)
    # a34ef34 regression signature: error / "Connection error." / empty text. PROVEN = a real caption, no error, on the
    # metered path, AND the ANTHROPIC vendor actually answered (not a bandit substitute) — else the proof is void.
    proven = ((not r.get("error")) and bool(text) and r.get("executor") == "api"
              and served == prov and not substituted)
    print(json.dumps({"phase": "live", "model": model, "provider": prov, "served_by": served,
                      "substituted": substituted, "timeout_s": timeout_s,
                      "error": r.get("error"), "error_type": r.get("error_type"), "executor": r.get("executor"),
                      "caption": text[:300], "caption_len": len(text),
                      "in_tok": r.get("in_tok"), "out_tok": r.get("out_tok"),
                      "billed_usd": (round(r["cost"], 6) if r.get("cost") is not None else None),
                      "latency_s": (round(r["latency"], 2) if r.get("latency") is not None else None),
                      "VISION_TIMEOUT_PROVEN": proven,
                      "note": ("real Anthropic SDK returned a caption for a vision call built WITH timeout_s — the "
                               "a34ef34 fix holds end to end (no 'Connection error.', no None)" if proven else
                               "NOT proven — inspect error/served_by/substituted/executor: a 'Connection error.' means "
                               "the httpx-timeout-on-vision trap regressed; served_by != anthropic means it wasn't "
                               "pinned and the proof exercised the wrong SDK")}, indent=2))
    return 0 if proven else 4


def main():
    import spendguard
    spendguard.require()                                   # fail closed: halts unless the gate is ENFORCING here

    ap = argparse.ArgumentParser(description="live proof that timeout_s is safe on the Anthropic vision path (a34ef34)")
    ap.add_argument("--model", default=DEFAULT_VISION_MODEL, help="vision model id (default: the model the trap hit)")
    ap.add_argument("--cap", type=float, default=DEFAULT_PROOF_CAP_USD, help="refuse if the estimate exceeds this $")
    ap.add_argument("--out-cap", type=int, default=DEFAULT_OUT_CAP_TOKENS, help="output-token ceiling for the caption")
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S, help="the timeout_s under test (the trap's value)")
    ap.add_argument("--intent", default=DEFAULT_INTENT, help="attribution label for the proof spend")
    ap.add_argument("--live", action="store_true", help="make the ONE real vision call; omit for the $0 estimate")
    a = ap.parse_args()

    if not a.live:
        return estimate_only(a.model, a.cap, a.out_cap)
    return run_live(a.model, a.cap, a.out_cap, a.timeout_s, a.intent)


if __name__ == "__main__":
    sys.exit(main())
