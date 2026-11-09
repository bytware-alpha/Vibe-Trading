"""Decimal parsing for IB's stringly-typed money fields."""

from __future__ import annotations

import math
import re
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

UNSET_ABS = Decimal(10000000000)
CENTS = Decimal("0.01")
QTY_QUANTUM = Decimal("0.0001")
_NUMBER = re.compile(r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,4})?$")


def parse_money(value: object) -> Decimal | None:
    """Parse an IB money field. Blank and the unset sentinel become None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value) or abs(value) >= 1e10):
            return None
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
    else:
        text = str(value).strip().replace("\u2212", "-").replace(",", "").replace(" ", "")
        if not text:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        text = re.sub(r"[A-Za-z]+$", "", text)
        if not text or text in {"-", ".", "-."}:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        if negative:
            number = -number
    if not number.is_finite() or abs(number) >= UNSET_ABS:
        return None
    return number


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def num_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def money_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return num_str(money(value))


def qty_str(value: Decimal) -> str:
    quantum = value.quantize(QTY_QUANTUM, rounding=ROUND_HALF_UP)
    return num_str(quantum)


def held_limit(raw: Decimal) -> Decimal:
    """Round a long down and a short away from zero so a close cannot oversell."""
    if raw < 0:
        return raw.quantize(QTY_QUANTUM, rounding=ROUND_FLOOR)
    return raw.quantize(QTY_QUANTUM, rounding=ROUND_DOWN)


def parse_plain_number(value: object, name: str) -> str:
    """Return the canonical text of a JSON or CLI number, or raise ValueError."""
    if isinstance(value, bool) or value is None:
        raise ValueError(name)
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(name)
        text = format(Decimal(str(value)), "f")
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TypeError(name)
    if not _NUMBER.fullmatch(text):
        raise ValueError(name)
    return text
