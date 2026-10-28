"""The single money scale for the whole package: integer nano-USD per USD.

Round to the nearest nano-USD using ROUND_HALF_EVEN (banker's rounding).
Only sub-nano input is quantized; this floor is 1000 times finer than the old
micro-USD floor. SQLite signed INTEGER holds roughly 9.22 billion USD at this
scale; conversions for storage reject amounts outside its full signed range.
"""

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Context,
    Decimal,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)


NANO_USD_PER_USD = 1_000_000_000
_NANO_USD_DECIMAL_PLACES = len(str(NANO_USD_PER_USD)) - 1
_NANO_SQLITE_SIGNED_BITS = 63
_NANO_SQLITE_MAX = (1 << _NANO_SQLITE_SIGNED_BITS) - 1
_NANO_SQLITE_MIN = -(_NANO_SQLITE_MAX + 1)
_NANO_SQLITE_USD_DIGITS = len(str(_NANO_SQLITE_MAX)) - _NANO_USD_DECIMAL_PLACES
_NANO_TOTAL_DEFAULT_TOLERANCE_USD = "0.01"


def usd_to_nano_usd(value: Decimal | str) -> int:
    """Convert exact decimal dollars to a SQLite-safe integer nano-USD amount.

    Round to the nearest nano-USD using ROUND_HALF_EVEN (banker's rounding).
    Only sub-nano input rounds, at a floor 1000 times finer than micro-USD.
    Floats are forbidden here: use real_usd_to_nano_usd for REAL-column data.
    Non-finite values raise ValueError; out-of-range results raise OverflowError.
    Decimal exponent shifting is exact regardless of the caller's context.
    """
    if not isinstance(value, (Decimal, str)):
        raise TypeError("exact USD must be a Decimal or decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("USD must be a valid finite decimal amount") from exc
    if not amount.is_finite():
        raise ValueError("USD amount must be finite (NaN and Infinity are forbidden)")
    if amount.is_zero():
        return 0
    # Reject obviously huge values before shifting a maximal Decimal exponent.
    if amount.adjusted() >= _NANO_SQLITE_USD_DIGITS:
        raise OverflowError("nano-USD amount exceeds SQLite signed 64-bit INTEGER range")
    sign, digits, exponent = amount.as_tuple()
    scaled = Decimal((sign, digits, exponent + _NANO_USD_DECIMAL_PLACES))
    rounded = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    if not _NANO_SQLITE_MIN <= rounded <= _NANO_SQLITE_MAX:
        raise OverflowError("nano-USD amount exceeds SQLite signed 64-bit INTEGER range")
    return int(rounded)


def nano_usd_to_usd(nano: int) -> Decimal:
    """Return exact Decimal dollars, including for Python integer aggregate totals."""
    if not isinstance(nano, int) or isinstance(nano, bool):
        raise TypeError("nano-USD must be an integer")
    sign, digits, exponent = Decimal(nano).as_tuple()
    return Decimal((sign, digits, exponent - _NANO_USD_DECIMAL_PLACES))


def real_usd_to_nano_usd(float_value: float) -> int:
    """Convert REAL-column USD via Decimal(str(x)), then the exact converter.

    This preserves the float's decimal representation, not precision already lost
    in its original binary storage. The same rounding and range guards apply.
    """
    if not isinstance(float_value, float):
        raise TypeError("REAL USD must be a float")
    return usd_to_nano_usd(Decimal(str(float_value)))


def assert_nano_total_matches(
    nano_total: int,
    reference_usd: Decimal,
    *,
    tolerance_usd: str = _NANO_TOTAL_DEFAULT_TOLERANCE_USD,
    label: str,
) -> Decimal:
    """Return signed (nano total - reference) USD, or fail loudly beyond tolerance.

    The comparison is exact and inclusive at the tolerance boundary; neither the
    reference nor the discrepancy is quantized. Non-finite inputs are rejected.
    """
    actual_usd = nano_usd_to_usd(nano_total)
    if not isinstance(reference_usd, Decimal):
        raise TypeError(f"{label}: reference USD must be a Decimal")
    if not reference_usd.is_finite():
        raise ValueError(f"{label}: reference USD must be finite")
    if not isinstance(tolerance_usd, str):
        raise TypeError(f"{label}: tolerance USD must be a decimal string")
    try:
        tolerance = Decimal(tolerance_usd)
    except InvalidOperation as exc:
        raise ValueError(f"{label}: tolerance USD must be a finite nonnegative decimal") from exc
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError(f"{label}: tolerance USD must be finite and nonnegative")
    # Enough coefficient digits for aligned subtraction plus a possible carry.
    lowest_exponent = min(actual_usd.as_tuple().exponent, reference_usd.as_tuple().exponent)
    highest_place = max(actual_usd.adjusted(), reference_usd.adjusted())
    carry_digits = 2
    delta_context = Context(
        prec=max(carry_digits, highest_place - lowest_exponent + carry_digits),
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
    )
    with localcontext(delta_context):
        delta = actual_usd - reference_usd
    if delta.copy_abs() > tolerance:
        raise AssertionError(
            f"{label}: nano total USD {actual_usd} differs from reference USD "
            f"{reference_usd} by {delta}; tolerance USD {tolerance}"
        )
    return delta
