"""Lossless, backwards-compatible compression for locally retained text."""
import base64
import zlib


COMPRESSED_TEXT_PREFIX = "spendguard:zlib:v1:"
COMPRESSION_LEVEL = 9


def compress_retained_text(value):
    """Return a TEXT-safe compressed representation without dropping any input."""
    if value is None or not isinstance(value, str) or not value or value.startswith(COMPRESSED_TEXT_PREFIX):
        return value
    payload = zlib.compress(value.encode("utf-8"), COMPRESSION_LEVEL)
    return COMPRESSED_TEXT_PREFIX + base64.b64encode(payload).decode("ascii")


def decompress_retained_text(value):
    """Decode compressed TEXT and pass legacy plaintext through unchanged."""
    if not isinstance(value, str) or not value.startswith(COMPRESSED_TEXT_PREFIX):
        return value
    payload = base64.b64decode(value[len(COMPRESSED_TEXT_PREFIX):], validate=True)
    return zlib.decompress(payload).decode("utf-8")


def ledger_text_factory(raw_value):
    """SQLite text factory providing transparent compressed and legacy reads."""
    return decompress_retained_text(raw_value.decode("utf-8"))
