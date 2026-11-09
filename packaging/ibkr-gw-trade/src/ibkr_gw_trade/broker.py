"""ib_async session. readonly=False is used only for what-if and place."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from ibkr_gw_trade.config import Settings
from ibkr_gw_trade.errors import BrokerError, TradeError
from ibkr_gw_trade.gates import OrderSpec

LOCK_WAIT_SECONDS = 60


def clean_text(value: object, limit: int = 240) -> str:
    text = "".join(ch if str(ch).isprintable() else " " for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def broker_message(exc: BaseException) -> str:
    text = clean_text(exc, 300)
    lowered = text.lower()
    if "321" in text or "read-only" in lowered or "read only" in lowered:
        return (
            "gateway rejected the call because the API session is read-only. "
            "Leave Read-Only API unchecked in IB Gateway."
        )
    if not text:
        return "gateway error"
    return f"gateway error: {text}"


def ib_number(value: Decimal) -> int | float:
    quantum = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if quantum == quantum.to_integral_value():
        return int(quantum)
    return float(quantum)


def default_ib_factory():
    """Construct an unconnected IB client. The socket opens only in connect()."""
    from ib_async import IB

    return IB()


def default_contract_builder(spec: OrderSpec):
    from ib_async import Contract, Option, Stock

    if spec.con_id and not spec.symbol:
        contract = Contract(conId=spec.con_id, exchange=spec.exchange, currency=spec.currency)
        contract.secType = spec.sec_type
        return contract
    if spec.sec_type == "STK":
        contract = Stock(spec.symbol, spec.exchange, spec.currency)
    elif spec.sec_type == "OPT":
        contract = Option(
            spec.symbol,
            spec.expiry,
            float(spec.strike or 0),
            spec.right,
            spec.exchange,
            currency=spec.currency,
        )
    else:
        raise TradeError("bad_sec_type", "secType must be STK or OPT")
    if spec.con_id:
        contract.conId = spec.con_id
    return contract


def default_order_builder(spec: OrderSpec, account: str):
    from ib_async import LimitOrder

    order = LimitOrder(
        spec.side,
        ib_number(spec.qty),
        ib_number(spec.limit),
        tif=spec.tif,
        account=account,
        transmit=True,
    )
    order.orderRef = "ibkr-gw-trade"
    if getattr(order, "orderType", None) != "LMT":
        raise TradeError("not_limit", "only LIMIT orders can be sent")
    return order


def execution_filter(account: str, day: date):
    time_text = day.strftime("%Y%m%d-00:00:00")
    try:
        from ib_async import ExecutionFilter
    except ImportError:
        return SimpleNamespace(acctCode=account, time=time_text)
    filt = ExecutionFilter()
    filt.acctCode = account
    filt.time = time_text
    return filt


class IbBroker:
    """Factory for short-lived gateway sessions.

    Public service methods hold ``exclusive()`` so client ids 17 and 18 are
    not opened twice in this process.
    """

    def __init__(self, settings: Settings, *, ib_factory=None, contract_builder=None, order_builder=None) -> None:
        import threading

        self.settings = settings
        self.ib_factory = ib_factory or default_ib_factory
        self.contract_builder = contract_builder or default_contract_builder
        self.order_builder = order_builder or default_order_builder
        self.lock = threading.Lock()

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise BrokerError("gateway connection is busy")
        try:
            yield
        finally:
            self.lock.release()

    def connect(self, *, trading: bool) -> IbSession:
        readonly = not trading
        client_id = self.settings.trade_client_id if trading else self.settings.read_client_id
        if trading and readonly:
            raise TradeError("readonly_path", "trading connection must not be read-only")
        if not trading and not readonly:
            raise TradeError("readonly_path", "read connection must stay read-only")
        ib = self.ib_factory()
        try:
            ib.connect(
                self.settings.host,
                int(self.settings.port),
                clientId=int(client_id),
                timeout=float(self.settings.timeout),
                readonly=readonly,
                account=self.settings.account,
            )
        except TradeError:
            raise
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        return IbSession(ib=ib, broker=self, trading=trading, readonly=readonly)


class IbSession:
    def __init__(self, *, ib, broker: IbBroker, trading: bool, readonly: bool) -> None:
        self.ib = ib
        self.broker = broker
        self.trading = trading
        self.readonly = readonly

    def disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:  # noqa: BLE001 - a failed disconnect must not hide the trading result
            return

    def managed_accounts(self) -> list[str]:
        try:
            rows = self.ib.managedAccounts() or []
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        return [str(item) for item in rows]

    def assert_account(self) -> None:
        account = self.broker.settings.account
        if account not in self.managed_accounts():
            raise TradeError("account_not_visible", "configured account is not in this gateway session")

    def account_summary(self):
        self.assert_account()
        try:
            return list(self.ib.accountSummary(self.broker.settings.account) or [])
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc

    def positions(self):
        self.assert_account()
        account = self.broker.settings.account
        try:
            rows = list(self.ib.positions() or [])
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        return [row for row in rows if getattr(row, "account", None) == account]

    def qualify(self, spec: OrderSpec):
        contract = self.broker.contract_builder(spec)
        try:
            qualified = self.ib.qualifyContracts(contract)
        except TradeError:
            raise
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        if not qualified:
            raise TradeError("contract_not_qualified", "gateway did not qualify the contract")
        contract = qualified[0]
        con_id = int(getattr(contract, "conId", 0) or 0)
        if con_id <= 0:
            raise TradeError("contract_not_qualified", "qualified contract has no conId")
        if spec.con_id and con_id != spec.con_id:
            raise TradeError("contract_mismatch", "conId does not match the request")
        sec_type = str(getattr(contract, "secType", "") or spec.sec_type).upper()
        if sec_type != spec.sec_type:
            raise TradeError("contract_mismatch", "qualified security type does not match the request")
        if spec.sec_type == "OPT":
            _assert_option_identity(contract, spec)
        return contract

    def what_if(self, contract, spec: OrderSpec):
        order = self._checked_order(spec)
        try:
            state = self.ib.whatIfOrder(contract, order)
        except TradeError:
            raise
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        return state

    def place(self, contract, spec: OrderSpec):
        order = self._checked_order(spec)
        try:
            return self.ib.placeOrder(contract, order)
        except TradeError:
            raise
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc

    def open_orders(self):
        self.assert_account()
        account = self.broker.settings.account
        try:
            if hasattr(self.ib, "reqAllOpenOrders"):
                self.ib.reqAllOpenOrders()
            if not hasattr(self.ib, "openTrades"):
                raise BrokerError("ib_async IB.openTrades is not available")
            rows = list(self.ib.openTrades() or [])
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        selected = []
        for trade in rows:
            order = getattr(trade, "order", None)
            if getattr(order, "account", "") != account:
                continue
            selected.append(trade)
        return selected

    def executions(self, day: date):
        self.assert_account()
        account = self.broker.settings.account
        try:
            if hasattr(self.ib, "reqExecutions"):
                return list(self.ib.reqExecutions(execution_filter(account, day)) or [])
            if hasattr(self.ib, "fills"):
                return list(self.ib.fills() or [])
        except Exception as exc:
            raise BrokerError(broker_message(exc)) from exc
        raise BrokerError("ib_async IB.reqExecutions is not available")

    def _checked_order(self, spec: OrderSpec):
        if self.readonly or not self.trading:
            raise TradeError("readonly_path", "orders require the trading connection")
        order = self.broker.order_builder(spec, self.broker.settings.account)
        if getattr(order, "orderType", None) != "LMT":
            raise TradeError("not_limit", "only LIMIT orders can be sent")
        if getattr(order, "tif", None) not in {"DAY", "GTC"}:
            raise TradeError("bad_tif", "tif must be DAY or GTC")
        if getattr(order, "action", None) not in {"BUY", "SELL"}:
            raise TradeError("bad_side", "side must be BUY or SELL")
        if getattr(order, "account", None) != self.broker.settings.account:
            raise TradeError("account_not_configured", "order account does not match the config")
        if getattr(order, "transmit", True) is not True:
            raise TradeError("not_limit", "only a transmitting limit order is allowed")
        return order


def _assert_option_identity(contract, spec: OrderSpec) -> None:
    expiry = _expiry_of(contract)
    right = str(getattr(contract, "right", "") or "").strip().upper()
    if right == "CALL":
        right = "C"
    if right == "PUT":
        right = "P"
    strike_raw = getattr(contract, "strike", None)
    if not expiry or right not in {"C", "P"} or strike_raw in (None, ""):
        raise TradeError("contract_not_qualified", "qualified option is incomplete")
    if expiry != spec.expiry or right != spec.right or Decimal(str(strike_raw)) != spec.strike:
        raise TradeError("contract_mismatch", "qualified option does not match the request")


def _expiry_of(contract) -> str:
    raw = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
    digits = re.sub(r"\D", "", raw)
    return digits[:8]
