"""Preview, confirm, and one-order submit. No batch and no market orders."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ibkr_gw_trade import __version__
from ibkr_gw_trade.audit import AuditLog, hash_prefix, iso_time
from ibkr_gw_trade.broker import IbBroker, clean_text
from ibkr_gw_trade.config import Settings
from ibkr_gw_trade.confirms import ConfirmStore, diff_snapshot, sign
from ibkr_gw_trade.errors import BrokerError, TradeError
from ibkr_gw_trade.gates import (
    SUMMARY_TAGS,
    OrderSpec,
    freeze_snapshot,
    parse_order_spec,
    position_gate,
    project_liquidity,
    read_sma,
    risk_gate,
    sma_impact,
)
from ibkr_gw_trade.money import held_limit, money, money_str, parse_money, qty_str

_AUDIT_FROM_SNAPSHOT = (
    "account",
    "availableFunds",
    "commission",
    "conId",
    "excessLiquidity",
    "initMarginAfter",
    "initMarginBefore",
    "limit",
    "localSymbol",
    "maintMarginAfter",
    "maintMarginBefore",
    "positionQty",
    "projectedAvailableFunds",
    "projectedExcessLiquidity",
    "projectedSma",
    "qty",
    "secType",
    "side",
    "sma",
    "smaBuffer",
    "smaImpact",
    "symbol",
    "tif",
    "warning",
)


@dataclass
class _Read:
    summary: dict[str, str | None]
    contract: object
    held: Decimal
    local_symbol: str
    con_id: int


@dataclass
class _Evaluation:
    snapshot: dict[str, str]
    reason_text: str
    warning: str
    refusal_code: str | None
    refusal_message: str


def preview_order(spec: OrderSpec, *, settings: Settings, broker: IbBroker, store: ConfirmStore, audit: AuditLog, now=None) -> dict:
    clock = _clock(now)
    with broker.exclusive():
        try:
            report = _preview_locked(spec, settings, broker, store, clock)
        except TradeError as exc:
            _audit_error(audit, "preview", exc, settings, clock())
            raise
        _audit_report(audit, "preview", report, settings, clock())
        return report


def submit_order(confirm: str, *, settings: Settings, broker: IbBroker, store: ConfirmStore, audit: AuditLog, now=None) -> dict:
    clock = _clock(now)
    with broker.exclusive():
        try:
            report = _submit_locked(confirm, settings, broker, store, clock)
        except TradeError as exc:
            _audit_error(audit, "submit", exc, settings, clock())
            raise
        _audit_report(audit, "submit", report, settings, clock())
        return report


def read_summary(*, settings: Settings, broker: IbBroker) -> dict:
    settings.require_account()
    with broker.exclusive():
        broker.settings = settings
        reader = broker.connect(trading=False)
        try:
            rows = reader.account_summary()
        finally:
            reader.disconnect()
    return {"account": settings.account, "summary": summary_rows(rows, settings.account)}


def read_positions(*, settings: Settings, broker: IbBroker) -> dict:
    settings.require_account()
    with broker.exclusive():
        broker.settings = settings
        reader = broker.connect(trading=False)
        try:
            rows = reader.positions()
        finally:
            reader.disconnect()
    return {"account": settings.account, "positions": position_rows(rows)}


def read_orders(*, settings: Settings, broker: IbBroker, audit: AuditLog, now=None) -> dict:
    clock = _clock(now)
    settings.require_account()
    with broker.exclusive():
        broker.settings = settings
        reader = broker.connect(trading=False)
        try:
            rows = reader.open_orders()
        finally:
            reader.disconnect()
        orders = [serialize_order(trade) for trade in rows]
        _audit_orders(audit, orders, settings, clock())
    return {"account": settings.account, "orders": orders}


def read_trades(*, settings: Settings, broker: IbBroker, audit: AuditLog, now=None) -> dict:
    clock = _clock(now)
    settings.require_account()
    day = local_today(clock())
    with broker.exclusive():
        broker.settings = settings
        reader = broker.connect(trading=False)
        try:
            rows = reader.executions(day)
        finally:
            reader.disconnect()
        trades, skipped = filter_trades(rows, settings.account, day)
        _audit_trades(audit, trades, settings, clock(), skipped)
    return {"account": settings.account, "date": day.isoformat(), "trades": trades, "skippedUndated": skipped}


def gateway_status(*, settings: Settings, broker: IbBroker) -> dict:
    body = {
        "ok": True,
        "bridge": "ibkr-gw-trade",
        "version": __version__,
        "account": settings.account,
        "host": settings.host,
        "port": settings.port,
        "readClientId": settings.read_client_id,
        "tradeClientId": settings.trade_client_id,
        "tradingEnabled": settings.enabled,
        "readonlyReads": True,
    }
    if not settings.account:
        body.update(connected=False, accountVisible=False, error="account_not_configured", message="account is not set")
        return body
    try:
        with broker.exclusive():
            broker.settings = settings
            reader = broker.connect(trading=False)
            try:
                accounts = reader.managed_accounts()
            finally:
                reader.disconnect()
    except TradeError as exc:
        body.update(connected=False, accountVisible=False, error=exc.code, message=str(exc))
        return body
    body.update(connected=True, accountVisible=settings.account in accounts)
    return body


def format_report(report: dict) -> str:
    snap = report["snapshot"]
    lines = [
        "PREVIEW ONLY. No order was placed.",
        f"account {snap['account']}",
        f"contract conId={snap['conId']} localSymbol={snap['localSymbol']} secType={snap['secType']}",
        f"side {snap['side']} qty {snap['qty']} LIMIT {snap['limit']} tif {snap['tif']}",
        f"est commission {snap['commission']} {snap['commissionCurrency']}".rstrip(),
        f"init margin before {snap['initMarginBefore']} after {snap['initMarginAfter']}",
        f"maint margin before {snap['maintMarginBefore']} after {snap['maintMarginAfter']}",
        f"equity with loan before {snap['equityWithLoanBefore']} after {snap['equityWithLoanAfter']}",
        f"equity with loan tag {snap['equityWithLoanValue'] or 'missing'}",
        (
            f"SMA {snap['sma'] or 'missing'} "
            f"ExcessLiquidity {snap['excessLiquidity'] or 'missing'} "
            f"AvailableFunds {snap['availableFunds'] or 'missing'}"
        ),
        f"sma impact {snap['smaImpact']} ({report.get('smaImpactReason') or ''})",
        f"projected SMA {snap['projectedSma'] or 'unavailable'} buffer {snap['smaBuffer']}",
        f"projected ExcessLiquidity {snap['projectedExcessLiquidity']}",
        f"projected AvailableFunds {snap['projectedAvailableFunds']}",
        f"position qty {snap['positionQty']}",
    ]
    if snap.get("warning"):
        lines.append(f"warning {snap['warning']}")
    reason = report.get("reason") or ""
    lines.append(f"decision {report.get('decision')} {reason}".rstrip())
    if report.get("confirm"):
        lines.append(f"confirm {report['confirm']} valid 120s single-use")
    else:
        lines.append("confirm withheld")
    return "\n".join(lines) + "\n"


def format_placement(report: dict) -> str:
    lines = [
        "LIVE ORDER PLACED.",
        f"account {report['account']}",
        f"orderId {report['orderId']} status {report['status']}",
        f"contract conId={report['conId']} localSymbol={report['localSymbol']}",
        f"side {report['side']} qty {report['qty']} LIMIT {report['limit']} tif {report['tif']}",
        "This confirm hash is spent.",
    ]
    return "\n".join(lines) + "\n"


def format_status(payload_orders: dict, payload_trades: dict) -> str:
    orders = payload_orders["orders"]
    trades = payload_trades["trades"]
    lines = [
        f"open orders {len(orders)}",
        f"trades {payload_trades['date']} {len(trades)}",
    ]
    for order in orders:
        lines.append(
            f"order {order['orderId']} {order['side']} {order['qty']} {order['localSymbol']} "
            f"LIMIT {order['limit']} {order['status']}"
        )
    for fill in trades:
        lines.append(f"fill {fill['execId']} {fill['side']} {fill['qty']} {fill['localSymbol']} {fill['price']} {fill['time']}")
    if not orders and not trades:
        lines.append("none")
    return "\n".join(lines) + "\n"


def summary_rows(rows, account: str) -> list[dict]:
    selected = []
    allowed = set(SUMMARY_TAGS)
    for row in rows:
        if getattr(row, "account", None) != account:
            continue
        tag = getattr(row, "tag", "")
        if tag not in allowed:
            continue
        selected.append(row)
    selected.sort(key=lambda row: SUMMARY_TAGS.index(getattr(row, "tag", "")))
    return [
        {
            "tag": row.tag,
            "value": "" if getattr(row, "value", None) is None else str(row.value),
            "currency": str(getattr(row, "currency", "") or ""),
        }
        for row in selected
    ]


def position_rows(rows) -> list[dict]:
    output = []
    for row in rows:
        contract = row.contract
        avg = parse_money(getattr(row, "avgCost", None))
        output.append(
            {
                "account": row.account,
                "conId": int(getattr(contract, "conId", 0) or 0),
                "symbol": str(getattr(contract, "symbol", "") or ""),
                "localSymbol": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
                "secType": str(getattr(contract, "secType", "") or ""),
                "currency": str(getattr(contract, "currency", "") or ""),
                "position": qty_str(Decimal(str(row.position))),
                "avgCost": money_str(avg) if avg is not None else "",
            }
        )
    return output


def serialize_order(trade) -> dict:
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)
    return {
        "orderId": _id_str(getattr(order, "orderId", "")),
        "account": str(getattr(order, "account", "") or ""),
        "conId": int(getattr(contract, "conId", 0) or 0),
        "localSymbol": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
        "secType": str(getattr(contract, "secType", "") or ""),
        "side": str(getattr(order, "action", "") or ""),
        "qty": _amount_str(getattr(order, "totalQuantity", "")),
        "limit": _amount_str(getattr(order, "lmtPrice", "")),
        "tif": str(getattr(order, "tif", "") or ""),
        "orderType": str(getattr(order, "orderType", "") or ""),
        "status": str(getattr(status, "status", "") or ""),
        "filled": _amount_str(getattr(status, "filled", "")),
        "remaining": _amount_str(getattr(status, "remaining", "")),
        "avgPrice": _amount_str(getattr(status, "avgFillPrice", "")),
    }


def filter_trades(rows, account: str, day: date) -> tuple[list[dict], int]:
    trades = []
    skipped = 0
    for fill in rows:
        execution = getattr(fill, "execution", fill)
        if str(getattr(execution, "acctNumber", "") or "") != account:
            continue
        raw_time = getattr(fill, "time", None)
        if raw_time in (None, ""):
            raw_time = getattr(execution, "time", None)
        if execution_day(raw_time) is None:
            skipped += 1
            continue
        if execution_day(raw_time) != day:
            continue
        contract = getattr(fill, "contract", None)
        trades.append(
            {
                "execId": clean_text(getattr(execution, "execId", "") or "", 80),
                "orderId": _id_str(getattr(execution, "orderId", "")),
                "account": account,
                "conId": int(getattr(contract, "conId", 0) or 0),
                "localSymbol": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
                "side": str(getattr(execution, "side", "") or ""),
                "qty": _amount_str(getattr(execution, "shares", "")),
                "price": _amount_str(getattr(execution, "price", "")),
                "time": time_text(raw_time),
            }
        )
    return trades, skipped


def execution_day(value) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().date()
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) < 8 or not text[:4].isdigit():
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def local_today(epoch: float) -> date:
    return datetime.fromtimestamp(epoch, UTC).astimezone().date()


def time_text(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return clean_text(value, 40)


def spec_from_snapshot(snapshot: dict) -> OrderSpec:
    payload = {
        "symbol": snapshot["symbol"],
        "secType": snapshot["secType"],
        "side": snapshot["side"],
        "qty": snapshot["qty"],
        "limit": snapshot["limit"],
        "tif": snapshot["tif"],
        "exchange": snapshot["exchange"],
        "currency": snapshot["currency"],
        "orderType": "LMT",
        "conId": snapshot["conId"],
        "multiplier": snapshot["multiplier"],
    }
    if snapshot.get("expiry"):
        payload["expiry"] = snapshot["expiry"]
    if snapshot.get("strike"):
        payload["strike"] = snapshot["strike"]
    if snapshot.get("right"):
        payload["right"] = snapshot["right"]
    return parse_order_spec(payload)


def _preview_locked(spec, settings, broker, store, clock) -> dict:
    settings.require_account()
    broker.settings = settings
    read = _read_and_qualify(spec, settings, broker)
    trader = broker.connect(trading=True)
    try:
        trader.assert_account()
        state = trader.what_if(read.contract, spec)
    finally:
        trader.disconnect()
    evaluation = _assemble(read, state, spec, settings)
    if evaluation.refusal_code:
        raise _refusal(evaluation)
    if not settings.enabled:
        return _report(
            evaluation,
            decision="withheld",
            reason="trading_disabled",
            message="trading is disabled; confirm withheld",
        )
    digest, expires = store.issue(evaluation.snapshot, clock())
    return _report(evaluation, decision="ok", reason=None, confirm=digest, expires=expires)


def _submit_locked(confirm, settings, broker, store, clock) -> dict:
    if not settings.enabled:
        raise TradeError("trading_disabled", "trading is disabled in config.json")
    settings.require_account()
    broker.settings = settings
    record = store.lookup(confirm, clock())
    spec = spec_from_snapshot(record.snapshot)
    read = _read_and_qualify(spec, settings, broker)
    trader = broker.connect(trading=True)
    try:
        trader.assert_account()
        state = trader.what_if(read.contract, spec)
        evaluation = _assemble(read, state, spec, settings)
        if evaluation.refusal_code:
            raise _refusal(evaluation)
        changes = diff_snapshot(record.snapshot, evaluation.snapshot)
        if changes:
            report = _report(evaluation, decision="refused", reason="confirm_mismatch")
            raise TradeError(
                "confirm_mismatch",
                "what-if changed: " + ", ".join(changes),
                details={"report": report, "hashPrefix": hash_prefix(confirm)},
            )
        if sign(record.snapshot, record.nonce) != record.digest:
            raise TradeError("hash_unknown", "unknown confirm")
        store.consume(confirm, clock())
        try:
            trade = trader.place(read.contract, spec)
        except TradeError as exc:
            if exc.code == "place_failed":
                raise
            raise TradeError(
                "place_failed",
                f"{exc} The confirm was consumed; check status before previewing again.",
                details={"hashPrefix": hash_prefix(confirm), "account": settings.account},
            ) from exc
        except Exception as exc:
            message = str(exc) if isinstance(exc, BrokerError) else "the gateway did not accept the order"
            raise TradeError(
                "place_failed",
                f"{message} The confirm was consumed; check status before previewing again.",
                details={"hashPrefix": hash_prefix(confirm), "account": settings.account},
            ) from exc
        if trade is None:
            raise TradeError(
                "place_failed",
                "the gateway did not accept the order. The confirm was consumed; check status before previewing again.",
                details={"hashPrefix": hash_prefix(confirm), "account": settings.account},
            )
        return _placement(trade, evaluation, confirm)
    finally:
        trader.disconnect()


def _read_and_qualify(spec: OrderSpec, settings: Settings, broker: IbBroker) -> _Read:
    reader = broker.connect(trading=False)
    try:
        reader.assert_account()
        summary = _index_summary(reader.account_summary(), settings.account)
        positions = reader.positions()
        contract = reader.qualify(spec)
        con_id = int(contract.conId)
        held = _held_qty(positions, con_id)
        local_symbol = _contract_text(contract, "localSymbol", _contract_text(contract, "symbol", spec.symbol))
        if not local_symbol and not spec.symbol:
            raise TradeError("contract_not_qualified", "qualified contract has no symbol")
        gate = position_gate(spec, held)
        if not gate.ok:
            raise TradeError(
                gate.code or "close_qty",
                gate.message,
                details={
                    "conId": str(con_id),
                    "localSymbol": local_symbol,
                    "secType": spec.sec_type,
                    "symbol": spec.symbol or _contract_text(contract, "symbol"),
                    "side": spec.side,
                    "qty": qty_str(spec.qty),
                    "limit": qty_str(spec.limit),
                    "tif": spec.tif,
                    "positionQty": qty_str(held),
                },
            )
        return _Read(summary=summary, contract=contract, held=held, local_symbol=local_symbol, con_id=con_id)
    finally:
        reader.disconnect()


def _assemble(read: _Read, state, spec: OrderSpec, settings: Settings) -> _Evaluation:
    what_if = _parse_what_if(state)
    multiplier = _resolved_multiplier(spec, read.contract)
    impact, reason = sma_impact(spec, multiplier)
    projected_el, projected_af = project_liquidity(
        what_if["equityWithLoanAfter"],
        what_if["initMarginAfter"],
        what_if["maintMarginAfter"],
    )
    sma_value, sma_status = read_sma(read.summary.get("SMA"))
    projected_sma = money(sma_value + impact) if sma_value is not None else None
    gate = risk_gate(
        side=spec.side,
        sma=sma_value,
        sma_status=sma_status,
        impact=impact,
        buffer=settings.sma_buffer,
        projected_el=projected_el,
        projected_af=projected_af,
    )
    symbol = _contract_text(read.contract, "symbol", spec.symbol)
    snapshot = freeze_snapshot(
        {
            "account": settings.account,
            "availableFunds": _tag_money(read.summary.get("AvailableFunds")),
            "commission": money_str(what_if["commission"]),
            "commissionCurrency": what_if["commissionCurrency"],
            "conId": str(read.con_id),
            "currency": _contract_text(read.contract, "currency", spec.currency),
            "equityWithLoanAfter": money_str(what_if["equityWithLoanAfter"]),
            "equityWithLoanBefore": money_str(what_if["equityWithLoanBefore"]),
            "equityWithLoanValue": _tag_money(read.summary.get("EquityWithLoanValue")),
            "excessLiquidity": _tag_money(read.summary.get("ExcessLiquidity")),
            "exchange": _contract_text(read.contract, "exchange", spec.exchange),
            "expiry": spec.expiry,
            "initMarginAfter": money_str(what_if["initMarginAfter"]),
            "initMarginBefore": money_str(what_if["initMarginBefore"]),
            "limit": qty_str(spec.limit),
            "localSymbol": read.local_symbol or symbol,
            "maintMarginAfter": money_str(what_if["maintMarginAfter"]),
            "maintMarginBefore": money_str(what_if["maintMarginBefore"]),
            "multiplier": qty_str(multiplier),
            "positionQty": qty_str(read.held),
            "projectedAvailableFunds": money_str(projected_af),
            "projectedExcessLiquidity": money_str(projected_el),
            "projectedSma": money_str(projected_sma),
            "right": spec.right,
            "secType": spec.sec_type,
            "side": spec.side,
            "sma": money_str(sma_value),
            "smaBuffer": money_str(settings.sma_buffer),
            "smaImpact": money_str(impact),
            "strike": qty_str(spec.strike) if spec.strike is not None else "",
            "symbol": symbol,
            "tif": spec.tif,
            "qty": qty_str(spec.qty),
            "warning": what_if["warning"],
            "whatIfStatus": what_if["status"],
        }
    )
    return _Evaluation(
        snapshot=snapshot,
        reason_text=reason,
        warning=what_if["warning"],
        refusal_code=gate.code,
        refusal_message=gate.message,
    )


def _parse_what_if(state) -> dict:
    if state is None:
        raise TradeError("what_if_incomplete", "what-if returned nothing")
    required = (
        "initMarginBefore",
        "initMarginAfter",
        "maintMarginBefore",
        "maintMarginAfter",
        "equityWithLoanBefore",
        "equityWithLoanAfter",
    )
    parsed = {}
    for name in required:
        number = parse_money(getattr(state, name, None))
        if number is None:
            raise TradeError("what_if_incomplete", f"what-if did not return {name}")
        parsed[name] = number
    commission = parse_money(getattr(state, "commission", None))
    if commission is None:
        commission = parse_money(getattr(state, "maxCommission", None))
    if commission is None:
        commission = parse_money(getattr(state, "minCommission", None))
    if commission is None:
        raise TradeError("what_if_incomplete", "what-if did not return a commission")
    parsed["commission"] = commission
    parsed["commissionCurrency"] = clean_text(getattr(state, "commissionCurrency", "") or "", 8)
    parsed["warning"] = clean_text(getattr(state, "warningText", "") or "")
    parsed["status"] = clean_text(getattr(state, "status", "") or "", 40)
    return parsed


def _resolved_multiplier(spec: OrderSpec, contract) -> Decimal:
    raw = getattr(contract, "multiplier", None)
    if raw not in (None, "", "0", 0):
        try:
            value = Decimal(str(raw).strip() or "0")
        except InvalidOperation as exc:
            raise TradeError("bad_multiplier", "qualified multiplier is not a number") from exc
    elif spec.multiplier:
        value = Decimal(spec.multiplier)
    elif spec.sec_type == "OPT":
        value = Decimal(100)
    else:
        value = Decimal(1)
    if spec.sec_type == "STK":
        return Decimal(1)
    if value <= 0 or value != value.to_integral_value():
        raise TradeError("bad_multiplier", "option multiplier must be a positive whole number")
    return value


def _index_summary(rows, account: str) -> dict[str, str | None]:
    found = {tag: [] for tag in SUMMARY_TAGS}
    for row in rows:
        if getattr(row, "account", None) != account:
            continue
        tag = getattr(row, "tag", "")
        if tag not in found:
            continue
        currency = str(getattr(row, "currency", "") or "")
        value = getattr(row, "value", "")
        found[tag].append((currency, "" if value is None else str(value)))
    output = {}
    preferred = {"USD", "BASE", ""}
    for tag, items in found.items():
        if not items:
            output[tag] = None
            continue
        pool = [value for currency, value in items if currency in preferred] or [value for _, value in items]
        if len(set(pool)) != 1:
            raise TradeError("ambiguous_tag", f"multiple values for {tag}")
        output[tag] = pool[0]
    return output


def _held_qty(rows, con_id: int) -> Decimal:
    total = Decimal(0)
    for row in rows:
        contract = row.contract
        if int(getattr(contract, "conId", 0) or 0) != con_id:
            continue
        total += Decimal(str(row.position))
    return held_limit(total)


def _tag_money(raw: str | None) -> str:
    if raw is None or str(raw).strip() == "":
        return ""
    parsed = parse_money(raw)
    if parsed is None:
        return ""
    return money_str(parsed)


def _contract_text(contract, name: str, fallback: str = "") -> str:
    value = getattr(contract, name, "") or ""
    text = str(value).strip()
    return text or fallback


def _report(evaluation: _Evaluation, *, decision: str, reason: str | None, message: str = "", confirm: str | None = None, expires: float | None = None) -> dict:
    return {
        "decision": decision,
        "reason": reason,
        "message": message or evaluation.refusal_message,
        "confirm": confirm,
        "confirmExpiresAt": iso_time(expires) if expires is not None else None,
        "snapshot": evaluation.snapshot,
        "smaImpactReason": evaluation.reason_text,
        "warning": evaluation.warning,
    }


def _refusal(evaluation: _Evaluation) -> TradeError:
    report = _report(evaluation, decision="refused", reason=evaluation.refusal_code)
    return TradeError(evaluation.refusal_code or "refused", evaluation.refusal_message, details={"report": report})


def _placement(trade, evaluation: _Evaluation, confirm: str) -> dict:
    order = serialize_order(trade)
    snap = evaluation.snapshot
    return {
        "decision": "placed",
        "reason": None,
        "message": "limit order placed",
        "hashPrefix": hash_prefix(confirm),
        "account": snap["account"],
        "conId": snap["conId"],
        "localSymbol": snap["localSymbol"],
        "secType": snap["secType"],
        "symbol": snap["symbol"],
        "side": snap["side"],
        "qty": snap["qty"],
        "limit": snap["limit"],
        "tif": snap["tif"],
        "orderId": order["orderId"],
        "status": order["status"],
        "filled": order["filled"],
        "remaining": order["remaining"],
        "avgPrice": order["avgPrice"],
        "smaImpactReason": evaluation.reason_text,
    }


def _audit_error(audit: AuditLog, event: str, exc: TradeError, settings: Settings, now: float) -> None:
    payload = {
        "event": event,
        "decision": "refused",
        "reason": exc.code,
        "account": settings.account,
        "tradingEnabled": settings.enabled,
    }
    report = exc.details.get("report")
    if isinstance(report, dict):
        payload.update(_flatten_report(report))
        payload["decision"] = "refused"
        payload["reason"] = exc.code
    for key in ("conId", "localSymbol", "secType", "symbol", "side", "qty", "limit", "tif", "positionQty", "hashPrefix"):
        if key in exc.details and exc.details[key] is not None:
            payload[key] = str(exc.details[key])
    audit.write(payload, now)


def _audit_report(audit: AuditLog, event: str, report: dict, settings: Settings, now: float) -> None:
    confirm = report.get("confirm")
    if isinstance(confirm, str):
        audit.forbid(confirm)
    payload = _flatten_report(report)
    payload["event"] = event
    payload["decision"] = report.get("decision")
    if report.get("reason"):
        payload["reason"] = report["reason"]
    payload["tradingEnabled"] = settings.enabled
    payload["account"] = report.get("account") or payload.get("account") or settings.account
    prefix = report.get("hashPrefix") or hash_prefix(confirm if isinstance(confirm, str) else None)
    if prefix:
        payload["hashPrefix"] = prefix
    for key in ("orderId", "status", "filled", "remaining", "avgPrice"):
        if report.get(key) not in (None, ""):
            payload[key] = str(report[key])
    audit.write(payload, now)


def _flatten_report(report: dict) -> dict:
    payload = {}
    snap = report.get("snapshot") or {}
    for key in _AUDIT_FROM_SNAPSHOT:
        if key in snap:
            payload[key] = snap[key]
    if report.get("smaImpactReason"):
        payload["smaImpactReason"] = report["smaImpactReason"]
    for key in ("orderId", "status", "filled", "remaining", "avgPrice", "account", "conId", "localSymbol", "side", "qty", "limit", "tif"):
        if key in report and key not in payload and report[key] not in (None, ""):
            payload[key] = str(report[key])
    return payload


def _audit_orders(audit: AuditLog, orders: list[dict], settings: Settings, now: float) -> None:
    if not orders:
        audit.write({"event": "status", "decision": "ok", "account": settings.account, "openOrders": 0}, now)
        return
    for order in orders:
        audit.write(
            {
                "event": "status",
                "decision": "ok",
                "account": order["account"],
                "conId": str(order["conId"]),
                "localSymbol": order["localSymbol"],
                "secType": order["secType"],
                "side": order["side"],
                "qty": order["qty"],
                "limit": order["limit"],
                "tif": order["tif"],
                "status": order["status"],
                "orderId": order["orderId"],
                "filled": order["filled"],
                "remaining": order["remaining"],
                "avgPrice": order["avgPrice"],
            },
            now,
        )


def _audit_trades(audit: AuditLog, trades: list[dict], settings: Settings, now: float, skipped: int) -> None:
    if not trades:
        payload = {"event": "fill", "decision": "ok", "account": settings.account, "fills": 0}
        if skipped:
            payload["warning"] = f"skipped undated {skipped}"
        audit.write(payload, now)
        return
    for fill in trades:
        audit.write(
            {
                "event": "fill",
                "decision": "ok",
                "account": fill["account"],
                "conId": str(fill["conId"]),
                "localSymbol": fill["localSymbol"],
                "side": fill["side"],
                "qty": fill["qty"],
                "price": fill["price"],
                "time": fill["time"],
                "orderId": fill["orderId"],
                "execId": fill["execId"],
            },
            now,
        )


def _clock(now):
    return now or time.time


def _amount_str(value) -> str:
    parsed = parse_money(value)
    if parsed is None:
        return "" if value in (None, "") else clean_text(value, 40)
    return qty_str(parsed)


def _id_str(value) -> str:
    if value in (None, ""):
        return ""
    parsed = parse_money(value)
    if parsed is None:
        return clean_text(value, 40)
    if parsed == parsed.to_integral_value():
        return str(int(parsed))
    return qty_str(parsed)
