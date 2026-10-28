"""Offline script regression checks for the nano-USD arithmetic foundation."""

from contextlib import contextmanager
from decimal import MAX_EMAX, Decimal, Inexact, Rounded, localcontext

from spendguard.money_scale import (
    NANO_USD_PER_USD,
    assert_nano_total_matches,
    nano_usd_to_usd,
    real_usd_to_nano_usd,
    usd_to_nano_usd,
)


NANO_EXACT_CASES = (
    ("0", 0),
    ("0.000000001", 1),
    ("0.0023", 2_300_000),
    ("1234567.89", 1_234_567_890_000_000),
)
NANO_QUANTIZATION_CASES = (
    ("0.0000000005", 0),
    ("0.0000000015", 2),
    ("0.0000000025", 2),
    ("0.0000000035", 4),
    ("0.00000000049", 0),
    ("0.00000000051", 1),
    ("0.0023000005", 2_300_000),
)
NANO_NONFINITE_CASES = ("NaN", "sNaN", "Infinity", "-Infinity")
NANO_REAL_CASES = (0.0, -0.0, 0.1, 0.1 + 0.2, -0.0023, 0.0000000005, 1234567.89)
NANO_INTEGER_BITS = 63
NANO_STORAGE_MAX = (1 << NANO_INTEGER_BITS) - 1
NANO_STORAGE_MIN = -(NANO_STORAGE_MAX + 1)
NANO_BOUNDARY_CASES = (
    ("9223372036.854775807", NANO_STORAGE_MAX),
    ("-9223372036.854775808", NANO_STORAGE_MIN),
    ("9223372036.8547758074", NANO_STORAGE_MAX),
    ("-9223372036.8547758085", NANO_STORAGE_MIN),
)
NANO_OVERFLOW_CASES = (
    "9223372036.8547758075",
    "-9223372036.8547758086",
    "9223372036.854775808",
    "-9223372036.854775809",
    "1e1000",
    "-1e1000",
)
NANO_TEST_LOW_PRECISION = 2
NANO_SUM_SAMPLE_COUNT = 10_000
NANO_SUM_AMOUNTS = ("0.000000001", "0.000000017", "-0.000000003", "0.000000101")
NANO_DRIFT_LABEL = "daily money rollup"
NANO_EXTREME_REFERENCE_USD = "1e1000000"
NANO_DRIFT_CASES = (
    ("1", "0"),
    ("0.99", "0.01"),
    ("1.01", "-0.01"),
    ("0.999999999999999999999999999999", "0.000000000000000000000000000001"),
)


@contextmanager
def expect_nano_foundation_error(error_type, message_fragment=""):
    try:
        yield
    except error_type as exc:
        assert message_fragment in str(exc), str(exc)
    else:
        raise AssertionError(f"expected {error_type.__name__}: {message_fragment}")


assert nano_usd_to_usd(NANO_USD_PER_USD) == Decimal("1")
for text, expected_nano in NANO_EXACT_CASES:
    for sign in (1, -1):
        amount = Decimal(text).copy_sign(Decimal(sign))
        assert usd_to_nano_usd(amount) == sign * expected_nano
        assert usd_to_nano_usd(str(amount)) == sign * expected_nano
        assert nano_usd_to_usd(sign * expected_nano) == amount

for text, expected_nano in NANO_QUANTIZATION_CASES:
    for sign in (1, -1):
        amount = Decimal(text).copy_sign(Decimal(sign))
        assert usd_to_nano_usd(amount) == sign * expected_nano
        assert nano_usd_to_usd(usd_to_nano_usd(amount)) == (
            Decimal(sign * expected_nano) / Decimal(NANO_USD_PER_USD)
        )

for text in NANO_NONFINITE_CASES:
    for value in (text, Decimal(text)):
        with expect_nano_foundation_error(ValueError, "finite"):
            usd_to_nano_usd(value)
for value in (0.1, 1, True, None):
    with expect_nano_foundation_error(TypeError):
        usd_to_nano_usd(value)
with expect_nano_foundation_error(ValueError):
    usd_to_nano_usd("invalid decimal")
for value in (0.1, "1", Decimal("1"), True):
    with expect_nano_foundation_error(TypeError):
        nano_usd_to_usd(value)

for text, expected_nano in NANO_BOUNDARY_CASES:
    assert usd_to_nano_usd(text) == expected_nano
for text in NANO_OVERFLOW_CASES:
    with expect_nano_foundation_error(OverflowError, "SQLite"):
        usd_to_nano_usd(text)
with expect_nano_foundation_error(OverflowError, "SQLite"):
    usd_to_nano_usd(Decimal((0, (1,), MAX_EMAX)))
assert usd_to_nano_usd(Decimal((0, (0,), MAX_EMAX))) == 0

for value in NANO_REAL_CASES:
    assert real_usd_to_nano_usd(value) == usd_to_nano_usd(Decimal(str(value)))
for text in NANO_NONFINITE_CASES:
    if text != "sNaN":  # Python floats have no signalling NaN representation.
        with expect_nano_foundation_error(ValueError, "finite"):
            real_usd_to_nano_usd(float(text))
with expect_nano_foundation_error(TypeError):
    real_usd_to_nano_usd(Decimal("0.1"))

for reference, expected_delta in NANO_DRIFT_CASES:
    assert assert_nano_total_matches(
        NANO_USD_PER_USD, Decimal(reference), label=NANO_DRIFT_LABEL
    ) == Decimal(expected_delta)
for reference in ("0.989999999999999999999999999999", "1.010000000000000000000000000001"):
    with expect_nano_foundation_error(AssertionError, NANO_DRIFT_LABEL):
        assert_nano_total_matches(NANO_USD_PER_USD, Decimal(reference), label=NANO_DRIFT_LABEL)
with expect_nano_foundation_error(AssertionError, NANO_DRIFT_LABEL):
    assert_nano_total_matches(0, Decimal(NANO_EXTREME_REFERENCE_USD), label=NANO_DRIFT_LABEL)
assert assert_nano_total_matches(
    0, Decimal("0"), tolerance_usd="0", label=NANO_DRIFT_LABEL
) == Decimal("0")
with expect_nano_foundation_error(AssertionError, NANO_DRIFT_LABEL):
    assert_nano_total_matches(1, Decimal("0"), tolerance_usd="0", label=NANO_DRIFT_LABEL)
for text in (*NANO_NONFINITE_CASES, "-0.01", "invalid decimal"):
    with expect_nano_foundation_error(ValueError, NANO_DRIFT_LABEL):
        assert_nano_total_matches(0, Decimal("0"), tolerance_usd=text, label=NANO_DRIFT_LABEL)
for text in NANO_NONFINITE_CASES:
    with expect_nano_foundation_error(ValueError, NANO_DRIFT_LABEL):
        assert_nano_total_matches(0, Decimal(text), label=NANO_DRIFT_LABEL)

# The caller's precision and rounding traps cannot change the money results.
with localcontext() as context:
    context.prec = NANO_TEST_LOW_PRECISION
    context.traps[Inexact] = True
    context.traps[Rounded] = True
    for text, expected_nano in (*NANO_EXACT_CASES, *NANO_QUANTIZATION_CASES, *NANO_BOUNDARY_CASES):
        assert usd_to_nano_usd(text) == expected_nano
    for text, expected_nano in NANO_EXACT_CASES:
        assert nano_usd_to_usd(expected_nano) == Decimal(text)
    for reference, expected_delta in NANO_DRIFT_CASES:
        assert assert_nano_total_matches(
            NANO_USD_PER_USD, Decimal(reference), label=NANO_DRIFT_LABEL
        ) == Decimal(expected_delta)

nano_samples = [
    Decimal(NANO_SUM_AMOUNTS[index % len(NANO_SUM_AMOUNTS)])
    for index in range(NANO_SUM_SAMPLE_COUNT)
]
assert nano_usd_to_usd(sum(usd_to_nano_usd(amount) for amount in nano_samples)) == sum(
    nano_samples, Decimal("0")
)
print(f"OK — nano-USD converters, drift guard, boundaries, and {NANO_SUM_SAMPLE_COUNT} exact sums")
