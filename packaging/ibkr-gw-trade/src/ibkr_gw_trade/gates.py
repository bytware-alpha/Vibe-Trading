"""Order shape, SMA impact, and the refusal gates. No gateway calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ibkr_gw_trade.errors import TradeError
from ibkr_gw_trade.money import money, money_str, parse_money, parse_plain_number, qty_str

SNAPSHOT_KEYS = (
    "account",
    "availableFunds",
    "commission",
    "commissionCurrency",
    "conId",
    "currency",
    "equityWithLoanAfter",
    "equityWithLoanBefore",
    "equityWithLoanValue",
    "excessLiquidity",
    "exchange",
    "expiry",
    "initMarginAfter",
    "initMarginBefore",
    "limit",
    "localSymbol",
    "maintMarginAfter",
    "maintMarginBefore",
    "multiplier",
    "positionQty",
    "projectedAvailableFunds",
    "projectedExcessLiquidity",
    "projectedSma",
    "right",
    "secType",
    "side",
    "sma",
    "smaBuffer",
    "smaImpact",
    "strike",
    "symbol",
    "tif",
    "qty",
    "warning",
    "whatIfStatus",
)
PREVIEW_KEYS = frozenset(
    {
        "symbol",
        "secType",
        "side",
        "qty",
        "limit",
        "tif",
        "exchange",
        "currency",
        "expiry",
        "strike",
        "right",
        "conId",
        "multiplier",
        "orderType",
    }
)
SUMMARY_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "ExcessLiquidity",
    "FullExcessLiquidity",
    "AvailableFunds",
    "InitMarginReq",
    "MaintMarginReq",
    "GrossPositionValue",
    "Cushion",
    "SMA",
    "EquityWithLoanValue",
    "RegTEquity",
    "RegTMargin",
    "Leverage",
)
_SEC_ALIASES = {"STK": "STK", "STOCK": "STK", "OPT": "OPT", "OPTION": "OPT"}
_RIGHT_ALIASES = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}
_SYMBOL = re.compile(r"^[A-Z0-9.]{1,12}$")
_EXCHANGE = re.compile(r"^[A-Z0-9]{1,16}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class OrderSpec:
    symbol: str
    sec_type: str
    side: str
    qty: Decimal
    limit: Decimal
    tif: str
    exchange: str
    currency: str
    expiry: str
    strike: Decimal | None
    right: str
    con_id: int | None
    multiplier: str


@dataclass(frozen=True)
class GateResult:
    code: str | None
    message: str

    @property
    def ok(self) -> bool:
        return self.code is None


def parse_order_spec(data: dict) -> OrderSpec:
    if not isinstance(data, dict):
        raise TradeError("batch_refused", "one order object is required")
    unknown = sorted(set(data) - PREVIEW_KEYS)
    if unknown:
        raise TradeError("unexpected_fields", "unexpected fields: " + ", ".join(unknown))
    order_type = str(data.get("orderType") or "LMT").strip().upper()
    if order_type in {"LIMIT"}:
        order_type = "LMT"
    if order_type != "LMT":
        raise TradeError("not_limit", "only LIMIT orders are allowed")
    sec_raw = str(data.get("secType") or "").strip().upper()
    sec_type = _SEC_ALIASES.get(sec_raw)
    if sec_type is None:
        raise TradeError("bad_sec_type", "secType must be STK or OPT")
    side = str(data.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        raise TradeError("bad_side", "side must be BUY or SELL")
    tif = str(data.get("tif") or "").strip().upper()
    if tif not in {"DAY", "GTC"}:
        raise TradeError("bad_tif", "tif must be DAY or GTC")
    try:
        qty = Decimal(parse_plain_number(data.get("qty"), "qty"))
        limit = Decimal(parse_plain_number(data.get("limit"), "limit"))
    except (TypeError, ValueError) as exc:
        field = str(exc)
        code = "bad_qty" if field == "qty" else "bad_price"
        raise TradeError(code, f"{field} must be a positive number with at most 4 decimal places") from exc
    if qty <= 0:
        raise TradeError("bad_qty", "qty must be positive")
    if limit <= 0:
        raise TradeError("bad_price", "limit must be positive")
    if sec_type == "OPT" and qty != qty.to_integral_value():
        raise TradeError("bad_qty", "option qty must be a whole number")
    symbol = str(data.get("symbol") or "").strip().upper()
    if symbol and not _SYMBOL.fullmatch(symbol):
        raise TradeError("bad_contract", "symbol is not usable")
    con_id = _parse_con_id(data.get("conId"))
    if not symbol and con_id is None:
        raise TradeError("bad_contract", "symbol or conId is required")
    exchange = str(data.get("exchange") or "SMART").strip().upper()
    currency = str(data.get("currency") or "USD").strip().upper()
    if not _EXCHANGE.fullmatch(exchange):
        raise TradeError("bad_contract", "exchange is not usable")
    if not _CURRENCY.fullmatch(currency):
        raise TradeError("bad_contract", "currency is not usable")
    expiry = _normalize_expiry(str(data.get("expiry") or ""))
    right = _normalize_right(str(data.get("right") or ""))
    strike = _parse_strike(data.get("strike"), sec_type)
    if sec_type == "STK" and (expiry or right or strike is not None):
        raise TradeError("bad_contract", "stock orders do not take expiry, strike, or right")
    if sec_type == "OPT":
        if not re.fullmatch(r"\d{8}", expiry):
            raise TradeError("bad_contract", "option expiry must be YYYYMMDD")
        if strike is None or strike <= 0:
            raise TradeError("bad_contract", "option strike must be positive")
        if right not in {"C", "P"}:
            raise TradeError("bad_contract", "option right must be C or P")
    multiplier = _parse_multiplier(data.get("multiplier"))
    return OrderSpec(
        symbol=symbol,
        sec_type=sec_type,
        side=side,
        qty=qty,
        limit=limit,
        tif=tif,
        exchange=exchange,
        currency=currency,
        expiry=expiry,
        strike=strike,
        right=right,
        con_id=con_id,
        multiplier=multiplier,
    )


def sma_impact(spec: OrderSpec, multiplier: Decimal) -> tuple[Decimal, str]:
    """Return the post-trade SMA change for a long-only stock or option order."""
    if spec.sec_type == "OPT":
        notional = money(spec.limit * spec.qty * multiplier)
        detail = f"limit {qty_str(spec.limit)} x qty {qty_str(spec.qty)} x multiplier {qty_str(multiplier)}"
        if spec.side == "BUY":
            return -notional, f"opening long option -100% of debit {money_str(notional)} ({detail})"
        return notional, f"selling long option +100% of credit {money_str(notional)} ({detail})"
    if spec.sec_type == "STK":
        value = money(spec.limit * spec.qty)
        half = money(value * Decimal("0.5"))
        detail = f"limit {qty_str(spec.limit)} x qty {qty_str(spec.qty)}"
        if spec.side == "BUY":
            return -half, f"stock buy -50% of value {money_str(value)} ({detail})"
        return half, f"stock sell +50% of value {money_str(value)} ({detail})"
    raise TradeError("bad_sec_type", "secType must be STK or OPT")


def position_gate(spec: OrderSpec, held: Decimal) -> GateResult:
    if spec.side == "BUY" and held < 0:
        return GateResult(
            "cover_unsupported",
            "buying to cover a short is refused; this tool only opens or adds to longs",
        )
    if spec.side == "SELL" and (held <= 0 or spec.qty > held):
        return GateResult(
            "close_qty",
            f"held {qty_str(held)} is below the sell qty {qty_str(spec.qty)}; refusing a short or naked option",
        )
    return GateResult(None, "")


def risk_gate(
    *,
    side: str,
    sma: Decimal | None,
    sma_status: str,
    impact: Decimal,
    buffer: Decimal,
    projected_el: Decimal,
    projected_af: Decimal,
) -> GateResult:
    if projected_el < 0:
        return GateResult(
            "excess_liquidity",
            f"projected ExcessLiquidity {money_str(projected_el)} would be negative",
        )
    if projected_af < 0:
        return GateResult(
            "available_funds",
            f"projected AvailableFunds {money_str(projected_af)} would be negative",
        )
    if side != "BUY":
        return GateResult(None, "")
    if sma_status == "unreadable":
        return GateResult("sma_unreadable", "the SMA tag is present but not a number")
    if sma is None or sma_status == "missing":
        return GateResult("sma_missing", "the SMA tag is missing; refusing an opening or buying order")
    projected = money(sma + impact)
    if projected < money(buffer):
        return GateResult(
            "sma_buffer",
            f"projected SMA {money_str(projected)} is below the buffer {money_str(buffer)}",
        )
    return GateResult(None, "")


def read_sma(raw: str | None) -> tuple[Decimal | None, str]:
    if raw is None or str(raw).strip() == "":
        return None, "missing"
    parsed = parse_money(raw)
    if parsed is None:
        return None, "unreadable"
    return money(parsed), "ok"


def freeze_snapshot(values: dict) -> dict[str, str]:
    missing = [key for key in SNAPSHOT_KEYS if key not in values]
    extra = [key for key in values if key not in SNAPSHOT_KEYS]
    if missing or extra:
        raise TradeError("internal", "snapshot fields drifted")
    frozen: dict[str, str] = {}
    for key in SNAPSHOT_KEYS:
        value = values[key]
        if not isinstance(value, str):
            raise TradeError("internal", f"snapshot {key} is not text")
        frozen[key] = value
    return frozen


def project_liquidity(
    equity_after: Decimal | None,
    init_after: Decimal | None,
    maint_after: Decimal | None,
) -> tuple[Decimal, Decimal]:
    if equity_after is None or init_after is None or maint_after is None:
        raise TradeError("what_if_incomplete", "what-if did not return equity and margin after the trade")
    return money(equity_after - maint_after), money(equity_after - init_after)


def _parse_con_id(value: object) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool):
        raise TradeError("bad_contract", "conId is not usable")
    try:
        text = str(value).strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", text):
            raise ValueError
        number = int(text)
    except ValueError as exc:
        raise TradeError("bad_contract", "conId is not usable") from exc
    return number


def _parse_strike(value: object, sec_type: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        text = parse_plain_number(value, "strike")
    except (TypeError, ValueError) as exc:
        raise TradeError("bad_contract", "strike is not usable") from exc
    number = Decimal(text)
    if sec_type != "OPT" and number == 0:
        return None
    return number


def _parse_multiplier(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        text = parse_plain_number(value, "multiplier")
    except (TypeError, ValueError) as exc:
        raise TradeError("bad_multiplier", "multiplier must be a positive whole number") from exc
    number = Decimal(text)
    if number <= 0 or number != number.to_integral_value():
        raise TradeError("bad_multiplier", "multiplier must be a positive whole number")
    return str(int(number))


def _normalize_expiry(value: str) -> str:
    digits = re.sub(r"\D", "", value.strip())
    if not digits:
        return ""
    return digits[:8]


def _normalize_right(value: str) -> str:
    text = value.strip().upper()
    if not text:
        return ""
    right = _RIGHT_ALIASES.get(text)
    if right is None:
        raise TradeError("bad_contract", "option right must be C or P")
    return right
