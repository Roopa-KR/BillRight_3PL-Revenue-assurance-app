"""Exact money primitives (P2).

Inputs: numbers as str/int/Decimal (never float).
Method: ``decimal.Decimal`` throughout; rounding to cents happens once, with ROUND_HALF_UP, at the
invoice-line level (customer x charge code x period) via ``round_cents``. Allocations use the
largest-remainder method so that cents always add up exactly to the pool.
Output: Decimal values.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 38
CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """Convert to Decimal. Floats are rejected: money must never pass through binary floating point."""
    if value is None:
        return None  # type: ignore[return-value]
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not allowed for money; pass str, int or Decimal")
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    return Decimal(str(value).strip().replace(",", ""))


def round_cents(value: Decimal) -> Decimal:
    """Round once to cents, ROUND_HALF_UP (e.g. 6734.676 -> 6734.68; 363.285 -> 363.29)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def largest_remainder(total: Decimal, weights: dict, unit: Decimal = CENT) -> dict:
    """Split ``total`` (already in cents) across keys in proportion to ``weights`` so the parts add up exactly.

    Each key first receives its share rounded DOWN to the unit; the remaining units go one at a time to the
    keys with the largest remainders (ties broken by key order, which the caller makes deterministic).
    Negative totals are split by their absolute value and the sign is restored.
    """
    total = D(total)
    if total != total.quantize(unit):
        raise ValueError("total must already be rounded to the unit")
    keys = list(weights)
    wsum = sum((D(weights[k]) for k in keys), ZERO)
    if not keys or wsum == 0:
        raise ValueError("no positive driver quantity to allocate on")
    sign = -1 if total < 0 else 1
    amount = abs(total)
    exact = {k: amount * D(weights[k]) / wsum for k in keys}
    floors = {k: exact[k].quantize(unit, rounding=ROUND_DOWN) for k in keys}
    left = int(((amount - sum(floors.values(), ZERO)) / unit).to_integral_value())
    order = sorted(keys, key=lambda k: (-(exact[k] - floors[k]), keys.index(k)))
    for k in order[:left]:
        floors[k] += unit
    return {k: floors[k] * sign for k in keys}


def pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """numerator / denominator x 100, full precision; None when the denominator is zero (P5)."""
    if denominator is None or D(denominator) == 0:
        return None
    return D(numerator) / D(denominator) * 100
