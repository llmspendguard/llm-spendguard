"""How many INPUT tokens a request's content is worth — images and documents included.

THE BUG THIS EXISTS TO KILL. Every estimator used to `json.dumps()` a message's content list and count the
result as text. For a vision request that means counting the **base64 payload** — roughly one token per two
characters of high-entropy data — instead of what the provider actually charges for the picture. Measured
against real Anthropic billing on 2026-07-30:

    200 panels, 448×448   estimated 10,174,860 in-tok · BILLED 234,300   → 26× over
    100 filmstrips, 2240×448  estimated 11,102,016 in-tok · BILLED 173,556 → 23× over

That is not a cosmetic number. The pre-spend cap is compared against it, so **every image batch is refused**
~25× too early and has to be chunked or waved through — which is exactly the moment a gate stops being used.

Providers do not charge for the bytes, they charge for the PIXELS:

  • **Anthropic** — `tokens ≈ (w × h) / 750`, after downscaling so the long edge is ≤ 1568px.
  • **OpenAI** — tiles: a flat base plus a per-512px-tile cost, after a fit-to-2048 then shortest-side-768
    rescale. `detail: "low"` is the base cost alone, no tiles.

Dimensions come from the image HEADER — the first few dozen bytes — not from decoding the payload: we
base64-decode a small prefix no matter how large the image is. Reading a `\\x89PNG…IHDR` width field is
mechanical format parsing, not a judgement, so it belongs in code (see CLAUDE.md: regex/parsing is for known
shapes; meaning goes to an LLM).

WHEN DIMENSIONS CANNOT BE READ (a remote URL we will not fetch, an unrecognised format) we fall back to a
documented flat per-image estimate and mark the basis `fallback` — we never fall back to measuring the
encoded string, because that is the bug. A gate whose estimate is wrong by 25× is worse than no gate: it
teaches you to override it.
"""
import base64
import binascii
import math
import struct
import sys

from . import config

# ── provider token geometry ───────────────────────────────────────────────────
# Named constants, not magic numbers. These are TOKEN-COUNTING rules (how many tokens a picture is worth),
# NOT prices — $/token stays in pricing.py and is never duplicated here.
ANTHROPIC_PX_PER_TOKEN = 750       # tokens ≈ (w × h) / 750
ANTHROPIC_MAX_EDGE = 1568          # images are downscaled so the LONG edge fits this before counting

OPENAI_TILE_PX = 512               # the grid the tile count is computed on
OPENAI_FIT_SQUARE = 2048           # first rescale: fit inside this square
OPENAI_SHORT_SIDE = 768            # second rescale: shortest side down to this
# (base, per-tile) by model family. 85/170 is the 4o/4.1/5 rule. gpt-4o-mini bills vision at a much larger
# multiple, and letting it default to 85/170 would under-estimate ~33× — the same class of bug in the other
# direction — so it is listed explicitly. Only families with a PUBLISHED multiplier belong here: an invented
# number is worse than the default, because it looks authoritative. Unknown models take the default.
OPENAI_TILE_COST_DEFAULT = (85, 170)
OPENAI_TILE_COST_BY_FAMILY = {"gpt-4o-mini": (2833, 5667)}
# Providers reached through an OpenAI-compatible endpoint bill vision the OpenAI way; this is the fallback for
# a provider that isn't in the adapters registry at all.
_DEFAULT_WIRE_KIND = "openai"

# Anthropic bills a PDF page as its rendered image + its extracted text; the published range is roughly
# 1,500–3,000 tokens per page. A gate must not under-estimate, so we take the TOP of that range.
PDF_TOKENS_PER_PAGE = 3000

_HEAD_STEPS = (512, 8192, 262144)  # decode this many base64-derived bytes, growing, until dimensions parse
_warned = set()


def _warn(msg):
    """One line per distinct problem — an estimator that warns on every request in an 800-item batch is an
    estimator people learn to ignore."""
    if msg not in _warned:
        _warned.add(msg)
        print(f"[spend_gate] {msg}", file=sys.stderr)


def _cfg_int(section, key, default):
    try:
        return int(config._cfg_get(section, key, default) or default)
    except Exception:
        return default


def fallback_image_tokens():
    """Per-image estimate when the pixels are genuinely unknowable. Same knob the claude.ai adapter uses
    (`chat.image_tokens`), so one setting governs every 'we cannot see the dimensions' path."""
    return _cfg_int("chat", "image_tokens", 1500)


# ── header parsing (format-known, no decoding of the whole payload) ───────────
def _png_dims(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
        return struct.unpack(">II", b[16:24])
    return None


def _gif_dims(b):
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", b[6:10])
    return None


def _webp_dims(b):
    if b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None
    chunk = b[12:16]
    if chunk == b"VP8X" and len(b) >= 30:
        w = int.from_bytes(b[24:27], "little") + 1
        h = int.from_bytes(b[27:30], "little") + 1
        return w, h
    if chunk == b"VP8 " and len(b) >= 30:
        return struct.unpack("<HH", b[26:30])
    if chunk == b"VP8L" and len(b) >= 25:
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _jpeg_dims(b):
    """Walk the marker segments to the frame header. EXIF thumbnails push SOF well past the first KB, which
    is why the caller grows the decoded prefix instead of assuming a fixed header size."""
    if b[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(b)
    while i + 9 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return None


def dims_from_bytes(b):
    """(width, height) from an image header, or None. Order is cheapest-signature-first."""
    for fn in (_png_dims, _gif_dims, _webp_dims, _jpeg_dims):
        try:
            d = fn(b)
        except Exception:
            d = None
        if d and d[0] and d[1]:
            return int(d[0]), int(d[1])
    return None


def _b64_prefix_bytes(data, nbytes):
    """Decode ~nbytes of a base64 string without touching the rest of it — the whole point is that a 12 MB
    image costs the same to measure as a 12 KB one."""
    chars = (nbytes * 4) // 3 + 4
    chunk = "".join(data[:chars].split())          # tolerate wrapped/whitespaced base64
    chunk = chunk[: len(chunk) - (len(chunk) % 4)]
    try:
        return binascii.a2b_base64(chunk)
    except Exception:
        return b""


def dims_from_b64(data):
    for step in _HEAD_STEPS:
        b = _b64_prefix_bytes(data, step)
        if not b:
            return None
        d = dims_from_bytes(b)
        if d:
            return d
        if len(b) < step:                          # we already hold the whole image; growing won't help
            return None
    return None


def _split_data_url(url):
    """('image/png', '<base64>') for a data: URL, else None. Format-known parsing, not a judgement."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    head, _, payload = url.partition(",")
    if ";base64" not in head:
        return None
    return head[5:].split(";", 1)[0].strip().lower() or "", payload


# ── the provider formulas ─────────────────────────────────────────────────────
def anthropic_image_tokens(w, h):
    if max(w, h) > ANTHROPIC_MAX_EDGE:             # the API downscales before charging; so must we
        scale = ANTHROPIC_MAX_EDGE / float(max(w, h))
        w, h = w * scale, h * scale
    return max(1, int(math.ceil((w * h) / ANTHROPIC_PX_PER_TOKEN)))


def _openai_tile_cost(model):
    """Exact lookup on the CANONICAL model id — `pricing.normalize` already owns 'which model is this really'
    (it strips date snapshots, -latest, -codex), and that is the same identity `price()` resolves on. Matching
    substrings here instead would invent a second, disagreeing notion of model identity."""
    if not model:
        return OPENAI_TILE_COST_DEFAULT
    try:
        from . import pricing
        canon = pricing.normalize(model)
    except Exception:
        canon = str(model).strip()
    return OPENAI_TILE_COST_BY_FAMILY.get(canon, OPENAI_TILE_COST_DEFAULT)


def openai_image_tokens(w, h, detail=None, model=None):
    base, per_tile = _openai_tile_cost(model)
    if (detail or "").lower() == "low":
        return base
    if max(w, h) > OPENAI_FIT_SQUARE:
        scale = OPENAI_FIT_SQUARE / float(max(w, h))
        w, h = w * scale, h * scale
    if min(w, h) > OPENAI_SHORT_SIDE:
        scale = OPENAI_SHORT_SIDE / float(min(w, h))
        w, h = w * scale, h * scale
    tiles = math.ceil(w / OPENAI_TILE_PX) * math.ceil(h / OPENAI_TILE_PX)
    return int(base + per_tile * tiles)


def _wire_kind(provider):
    """Which vision billing rule this provider uses, from `adapters.PROVIDERS[…]['kind']` — the registry that
    already answers 'which wire format is this provider'. An EXACT key lookup on a registry enum, never a
    substring guess: the caller is an interceptor installed on a specific SDK, so it knows its provider
    exactly, and a provider absent from the registry falls back to the documented default rather than being
    pattern-matched into a rule that may not apply."""
    if not provider:
        return _DEFAULT_WIRE_KIND
    try:
        from . import adapters
        spec = adapters.PROVIDERS.get(str(provider).strip().lower())
    except Exception:
        spec = None
    if spec is None:
        return _DEFAULT_WIRE_KIND
    return spec.get("kind") or _DEFAULT_WIRE_KIND


def image_tokens(w, h, provider=None, detail=None, model=None):
    """Tokens for ONE image of known pixel size. Anthropic and OpenAI charge differently enough (267 vs 850
    tokens for a 448×448 panel) that applying the wrong rule is itself a 3× error — callers pass the provider
    key they were installed for."""
    if _wire_kind(provider) == "anthropic":
        return anthropic_image_tokens(w, h)
    return openai_image_tokens(w, h, detail=detail, model=model)


def _pdf_pages(data):
    """Page count from a base64 PDF, or None. Cheap structural parse of a known format: prefer the page
    tree's /Count, fall back to counting /Type /Page objects."""
    try:
        raw = base64.b64decode("".join(data.split()), validate=False)
    except Exception:
        return None
    import re
    counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", raw)]
    if counts:
        return max(counts)
    n = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    return n or None


# ── the walker ────────────────────────────────────────────────────────────────
def _media_of(node):
    """(media_type, b64_data, detail) if this node IS a media payload, else None. Recognised by SHAPE —
    Anthropic's {type: base64, media_type, data} source, OpenAI's image_url / data: URL — never by guessing
    what a long string 'looks like'."""
    if isinstance(node, str):
        d = _split_data_url(node)
        return (d[0], d[1], None) if d else None
    if not isinstance(node, dict):
        return None
    if node.get("type") == "base64" and node.get("data") is not None:
        return (str(node.get("media_type") or ""), str(node.get("data") or ""), None)
    iu = node.get("image_url")
    if iu is not None:
        detail = node.get("detail")
        if isinstance(iu, dict):
            detail = iu.get("detail", detail)
            iu = iu.get("url")
        d = _split_data_url(iu) if isinstance(iu, str) else None
        return (d[0], d[1], detail) if d else ("", "", detail)     # remote URL → unknown pixels
    src = node.get("source")
    if isinstance(src, str):
        d = _split_data_url(src)
        return (d[0], d[1], node.get("detail")) if d else None
    if isinstance(src, dict):                       # Anthropic {type: image|document, source: {...}}
        inner = _media_of(src)
        if inner is not None:
            return (inner[0], inner[1], node.get("detail", inner[2]))
        if src.get("type") == "url":                # a URL we will not fetch → pixels unknown
            return ("", "", node.get("detail"))
    return None


def _walk(node, out):
    """Collect ('text', s) and ('media', (media_type, data, detail)) from ANY content shape. Recursive by
    design: tool_result blocks nest images, and new block types keep appearing — a walker that classifies by
    payload shape keeps working when the schema grows, instead of silently reverting to counting base64."""
    m = _media_of(node)
    if m is not None:
        out.append(("media", m))
        return
    if isinstance(node, str):
        out.append(("text", node))
    elif isinstance(node, dict):
        for v in node.values():
            # Only VALUES. The JSON key names are protocol framing, not prompt: the provider tokenizes the
            # content blocks, not the envelope that carried them. Counting them added a couple of tokens per
            # block — harmless in a total, but it destroys the exact identity
            # `count(text+image) - count(text) == image_tokens(w,h)`, which is what makes this testable.
            _walk(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk(v, out)


def count(content, provider=None, model=None, text_tokens=None):
    """INPUT tokens for a message's `content` (a plain string, or a list of blocks).

    `text_tokens` is the caller's text counter (tiktoken where available) so this module never has to own a
    tokenizer. Returns an int; see `count_detail` for the basis."""
    return count_detail(content, provider=provider, model=model, text_tokens=text_tokens)[0]


def count_detail(content, provider=None, model=None, text_tokens=None):
    """(tokens, detail) where detail = {images, images_measured, images_fallback, pdf_pages, text_tokens}.
    The counts are what makes a wrong estimate diagnosable instead of mysterious — `spendguard doctor` and
    the pre-spend line print them."""
    # `is not None`: this asks "did the caller SUPPLY a counter", and `or` answers "is the counter truthy",
    # which is a different question about a different thing. A callable is usually truthy so the gap is
    # narrow — a counter object defining __len__ or __bool__, or a test double — but the correct form costs
    # nothing and the wrong one substitutes a 4-chars-per-token guess for the caller's real tokenizer while
    # reporting the result as a measurement.
    tt = text_tokens if text_tokens is not None else (lambda s: max(1, len(s) // 4))
    parts = []
    _walk(content, parts)
    d = {"images": 0, "images_measured": 0, "images_fallback": 0, "pdf_pages": 0, "text_tokens": 0}
    total = 0
    text_buf = []
    for kind, val in parts:
        if kind == "text":
            text_buf.append(val)
            continue
        media_type, data, detail = val
        if "pdf" in (media_type or "") or (data and data[:20].startswith("JVBER")):   # %PDF- in base64
            pages = _pdf_pages(data) if data else None
            if not pages:
                pages = 1
                _warn("WARN could not read the PDF page count — estimating ONE page; the real cost scales "
                      "with pages (pass a page count, or split the document)")
            d["pdf_pages"] += pages
            total += pages * PDF_TOKENS_PER_PAGE
            continue
        d["images"] += 1
        dims = dims_from_b64(data) if data else None
        if dims:
            d["images_measured"] += 1
            total += image_tokens(dims[0], dims[1], provider=provider, detail=detail, model=model)
        else:
            d["images_fallback"] += 1
            total += fallback_image_tokens()
            _warn(f"WARN image dimensions unreadable ({media_type or 'remote url'}) — estimating "
                  f"{fallback_image_tokens()} tokens/image (config chat.image_tokens). NOT measuring the "
                  f"encoded bytes: that over-estimates ~25×.")
    if text_buf:
        d["text_tokens"] = tt("\n".join(text_buf))
        total += d["text_tokens"]
    return total, d


def has_media(content):
    """True if this content carries an image/document payload — lets a caller label an estimate honestly."""
    parts = []
    _walk(content, parts)
    return any(k == "media" for k, _v in parts)
