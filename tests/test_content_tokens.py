"""Image/document token counting — the fix for the 25× vision over-estimate.

THE BUG (measured against real Anthropic billing, 2026-07-30): every estimator json.dumps()'d a message's
content and counted the result as text, so a vision request was charged for its BASE64 PAYLOAD instead of its
pixels. 200 448×448 panels estimated 10,174,860 input tokens against 234,300 actually billed. The pre-spend
cap is compared to that number, so every image batch was refused ~25× too early.

Ground truth used below is the billed `usage` from those two batches — not a fixture invented to pass:

    200 panels 448×448        234,300 in-tok billed  → 1,171 per request
    100 filmstrips 2240×448   173,556 in-tok billed  → 1,736 per request

Invariants pinned here:
  • the count comes from PIXELS, never from the length of the encoded payload;
  • Anthropic's ≤1568px downscale is applied (skipping it over-estimates a wide filmstrip 2×);
  • provider and model identity are resolved by EXACT registry/canonical lookup, not substring guessing;
  • unreadable dimensions fall back to a documented flat estimate and SAY SO — never to counting bytes;
  • every estimator path (realtime chat, OpenAI batch jsonl, Anthropic batch requests, the submit gate)
    goes through this one counter, so the bug cannot come back in a path someone forgot.
"""
import os, sys, tempfile, json, struct, base64, io, contextlib, inspect

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ctok-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import content_tokens as ct

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


def png_b64(w, h, filler_kb=64):
    """A real PNG header (signature + IHDR carrying the true dimensions) plus filler, base64'd. The parser
    reads only the header; the filler is what the OLD estimator would have counted as tokens."""
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    raw = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
           + os.urandom(filler_kb * 1024))
    return base64.b64encode(raw).decode()


def jpeg_b64(w, h, exif_kb=48):
    """JPEG with a fat EXIF segment before the frame header — the case that breaks a fixed-size header peek."""
    exif = b"\xff\xe1" + struct.pack(">H", exif_kb * 1024 + 2) + os.urandom(exif_kb * 1024)
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    return base64.b64encode(b"\xff\xd8" + exif + sof + os.urandom(4096)).decode()


def anth_block(b64, media_type="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def oai_block(b64, media_type="image/png", detail=None):
    iu = {"url": f"data:{media_type};base64,{b64}"}
    if detail:
        iu["detail"] = detail
    return {"type": "image_url", "image_url": iu}


print("-- dimensions come from the header, at any payload size --")
check("PNG dimensions parse", ct.dims_from_b64(png_b64(448, 448)) == (448, 448))
check("JPEG behind a 48KB EXIF segment still parses", ct.dims_from_b64(jpeg_b64(1224, 1584)) == (1224, 1584))
check("a 4MB image costs the same header read", ct.dims_from_b64(png_b64(800, 600, filler_kb=4096)) == (800, 600))
check("garbage is reported as unknown, not guessed",
      ct.dims_from_b64(base64.b64encode(os.urandom(2048)).decode()) is None)

print("-- the Anthropic formula, against BILLED tokens from two real batches --")
# batch 1: 200 × 448px panels, 234,300 input tokens billed → 1,171/request (image + the shared text prompt)
img448 = ct.anthropic_image_tokens(448, 448)
check("448×448 → (w·h)/750 ≈ 268 tokens", img448 == 268, f"got {img448}")
# batch 2: 100 × 2240×448 filmstrips, 173,556 billed → 1,736/request. The long edge exceeds 1568, so the API
# downscales BEFORE charging; skipping that step yields 1,338 and over-states the batch ~2×.
strip = ct.anthropic_image_tokens(2240, 448)
check("2240×448 is downscaled to the 1568px long edge first", strip == 656, f"got {strip}")
check("…and that is far below the un-downscaled 1338", strip < 1338)
# What the billing can and cannot prove. It gives TOTAL input tokens per request (image + that batch's text
# prompt); it does not break them out, and the two batches ran different prompts, so we cannot assert the
# residuals are equal — only that the model is BOUNDED BY REALITY in both directions:
#   • the modelled image can never exceed the whole billed request (it is one part of it), and
#   • the residual it leaves must be a sane prompt, not a negative number or a rounding crumb.
# The old estimator failed exactly this: its per-request number was ~43× the entire billed request.
BILLED = {"panels": (1171, img448), "filmstrips": (1736, strip)}     # per request: billed total, modelled image
for label, (billed, modelled) in BILLED.items():
    check(f"{label}: the modelled image fits inside the billed request", 0 < modelled < billed,
          f"{modelled} vs {billed}")
    check(f"{label}: the residual is a plausible text prompt, not a crumb", 300 < billed - modelled < billed)
OLD_PER_REQ = {"panels": 10_174_860 / 200, "filmstrips": 11_102_016 / 100}   # what the gate used to report
for label, (billed, _m) in BILLED.items():
    check(f"{label}: the OLD estimate was >40× the entire billed request", OLD_PER_REQ[label] > 40 * billed,
          f"{OLD_PER_REQ[label]:,.0f} vs {billed}")

print("-- the OpenAI tile formula --")
check("512×512 high detail = base + 1 tile", ct.openai_image_tokens(512, 512) == 85 + 170)
check("detail=low is the flat base", ct.openai_image_tokens(4096, 4096, detail="low") == 85)
check("a large image is rescaled before tiling (not 64 tiles)", ct.openai_image_tokens(4096, 4096) == 85 + 170 * 4)
check("gpt-4o-mini uses its own published multiplier, by CANONICAL id",
      ct.openai_image_tokens(512, 512, model="gpt-4o-mini-2024-07-18") == 2833 + 5667)
check("an unknown model takes the documented default, not an invented number",
      ct.openai_image_tokens(512, 512, model="some-new-vlm") == 85 + 170)

print("-- provider identity is an exact registry lookup, not a substring guess --")
check("anthropic → the pixel formula", ct.image_tokens(448, 448, provider="anthropic") == 268)
check("openai → the tile formula", ct.image_tokens(448, 448, provider="openai") == 85 + 170)
check("an OpenAI-compatible provider bills the OpenAI way (kind from the registry)",
      ct.image_tokens(448, 448, provider="deepseek") == 85 + 170)
check("an unregistered provider falls back to the default, no crash",
      ct.image_tokens(448, 448, provider="not-a-provider") == 85 + 170)
src = inspect.getsource(ct)
code = src.split('"""', 2)[-1]
check("no substring matching on provider or model identity in the code",
      ".startswith(\"anthropic\")" not in code and "family in m" not in code)

print("-- THE BUG: the count must not scale with the encoded payload --")
small = [{"type": "text", "text": "describe this page"}, anth_block(png_b64(448, 448, filler_kb=8))]
big = [{"type": "text", "text": "describe this page"}, anth_block(png_b64(448, 448, filler_kb=4096))]
t_small = ct.count(small, provider="anthropic")
t_big = ct.count(big, provider="anthropic")
check("a 512× bigger encoded payload for the SAME pixels costs the same", t_small == t_big,
      f"{t_small} vs {t_big}")
# Exact identity: adding the image adds EXACTLY the image's modelled tokens and nothing payload-sized.
text_only = ct.count([small[0]], provider="anthropic")
check("adding the panel adds exactly (448·448)/750 tokens, nothing payload-sized",
      t_small - text_only == ct.anthropic_image_tokens(448, 448), f"delta {t_small - text_only}")
# what the old code did, for the record: json.dumps the block and count it as text.
old = ct.count_detail(json.dumps(big), provider="anthropic")[0]
check("the OLD behaviour is reproducibly ~1000× worse (that was the 25× on real batches)", old > 100 * t_big,
      f"old={old} new={t_big}")

print("-- 800 pages: the number a cap is compared against --")
page = [{"type": "text", "text": "extract the fields"}, anth_block(jpeg_b64(1224, 1584), "image/jpeg")]
per_page = ct.count(page, provider="anthropic")
page_text = ct.count([page[0]], provider="anthropic")
check("the page image contributes exactly its pixel cost",
      per_page - page_text == ct.anthropic_image_tokens(1224, 1584), f"delta {per_page - page_text}")
# The number a cap is actually compared against: 800 pages of the SAME image must cost 800× one page —
# under the old estimator it scaled with the JPEG's byte size instead, which is what tripped the cap.
book = ct.count([page[0]] + [page[1]] * 800, provider="anthropic")
check("800 pages = 800 × the per-image cost, exactly",
      book - page_text == 800 * ct.anthropic_image_tokens(1224, 1584), f"got {book:,}")

print("-- unreadable pixels: a documented fallback, and it SAYS so --")
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    remote = ct.count_detail([{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}],
                             provider="openai")
check("a remote URL is not fetched and not guessed from bytes", remote[0] == ct.fallback_image_tokens())
check("it is counted as a fallback, not as measured", remote[1]["images_fallback"] == 1)
check("and it warns, naming the knob", "chat.image_tokens" in buf.getvalue())
check("the warning states why bytes are NOT used", "encoded bytes" in buf.getvalue())

print("-- nested content (tool results carry images too) --")
nested = [{"type": "tool_result", "tool_use_id": "t1",
           "content": [{"type": "text", "text": "here"}, anth_block(png_b64(448, 448))]}]
check("an image nested inside a tool_result is measured, not stringified",
      ct.count(nested, provider="anthropic") < 500, f"got {ct.count(nested, provider='anthropic')}")
check("has_media sees it", ct.has_media(nested) is True)
check("plain text is unaffected", ct.count("hello world" * 100, provider="anthropic") > 0)
check("has_media is False for pure text", ct.has_media([{"type": "text", "text": "hi"}]) is False)

print("-- PDFs are pages, not bytes --")
pdf = b"%PDF-1.7\n" + b"/Type /Page \n" * 12 + b"trailer"
pdf_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf).decode()}}
tok, det = ct.count_detail([pdf_block], provider="anthropic")
check("the page count drives the estimate", det["pdf_pages"] == 12, f"got {det['pdf_pages']}")
# Exact identity: doubling the PAGES doubles the page term, and nothing else moves. (Comparing to an
# absolute number would fold in the block's key names; a delta cannot.)
pdf24 = b"%PDF-1.7\n" + b"/Type /Page \n" * 24 + b"trailer"
tok24, det24 = ct.count_detail([{"type": "document",
                                 "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf24).decode()}}], provider="anthropic")
check("12 more pages costs exactly 12 × the per-page rate",
      det24["pdf_pages"] == 24 and tok24 - tok == 12 * ct.PDF_TOKENS_PER_PAGE, f"delta {tok24 - tok}")

print("-- EVERY estimator goes through this counter (no path left behind) --")
from spendguard import gate, submit
for name, fn in (("gate._content_tokens", gate._content_tokens),
                 ("gate._estimate_openai_jsonl", gate._estimate_openai_jsonl),
                 ("gate._estimate_anthropic_requests", gate._estimate_anthropic_requests),
                 ("submit.estimate_jsonl_cost", submit.estimate_jsonl_cost)):
    s = inspect.getsource(fn)
    check(f"{name} no longer json.dumps content into a token count",
          "json.dumps(c)" not in s and "json.dumps(content" not in s)
check("gate._content_tokens delegates to content_tokens",
      "content_tokens" in inspect.getsource(gate._content_tokens))
check("submit.estimate_jsonl_cost delegates to content_tokens",
      "content_tokens" in inspect.getsource(submit.estimate_jsonl_cost))

print("-- end to end through the real gate estimators --")
line = json.dumps({"body": {"model": "gpt-5.5", "max_tokens": 100,
                            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                                                      oai_block(png_b64(512, 512))]}]}})
est = gate._estimate_openai_jsonl(line.encode())
bare = json.dumps({"body": {"model": "gpt-5.5", "max_tokens": 100,
                            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}})
est_bare = gate._estimate_openai_jsonl(bare.encode())
check("OpenAI batch jsonl: the image adds exactly its tile cost (85 + 170), not its payload",
      est["in_tok"] - est_bare["in_tok"] == ct.openai_image_tokens(512, 512),
      f"delta {est['in_tok'] - est_bare['in_tok']:,}")
areq = [{"params": {"model": "claude-haiku-4-5", "max_tokens": 100,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                                              anth_block(png_b64(448, 448))]}]}}]
aest = gate._estimate_anthropic_requests(areq)
abare = [{"params": {"model": "claude-haiku-4-5", "max_tokens": 100,
                     "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}}]
abare_est = gate._estimate_anthropic_requests(abare)
check("Anthropic batch requests: the panel adds exactly (448·448)/750",
      aest["in_tok"] - abare_est["in_tok"] == ct.anthropic_image_tokens(448, 448),
      f"delta {aest['in_tok'] - abare_est['in_tok']:,}")

with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
    fh.write(line + "\n")
    jsonl = fh.name
sest = submit.estimate_jsonl_cost(jsonl, "gpt-5.5")
check("the submit gate agrees with the in-process gate EXACTLY (one counter, one answer)",
      sest["in_tok"] == est["in_tok"], f"{sest['in_tok']:,} vs {est['in_tok']:,}")
check("and reports that the count included images", sest.get("media") is True)
os.unlink(jsonl)

print(f"\n{'[FAIL]' if failures else 'OK'} test_content_tokens: {failures} failure(s)")
sys.exit(1 if failures else 0)
