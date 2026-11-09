"""Fake IB and a wired runtime. Nothing here opens a socket."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from ibkr_gw_trade.audit import AuditLog
from ibkr_gw_trade.broker import IbBroker
from ibkr_gw_trade.config import init_config, load_settings
from ibkr_gw_trade.confirms import ConfirmStore
from ibkr_gw_trade.gates import parse_order_spec
from ibkr_gw_trade.service import preview_order, submit_order

ACCOUNT = "U23708642"


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    @property
    def day(self):
        return datetime.fromtimestamp(self.value, UTC).date()


def tag(account: str, name: str, value, currency: str = "USD"):
    return SimpleNamespace(account=account, tag=name, value=str(value), currency=currency, modelCode="")


def position(account: str, con_id: int, qty, symbol: str = "FIG", sec: str = "STK"):
    return SimpleNamespace(
        account=account,
        position=qty,
        avgCost=10,
        contract=SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol, secType=sec, currency="USD"),
    )


def what_if_state(**overrides):
    base = {
        "status": "PreSubmitted",
        "initMarginBefore": "1000.00",
        "initMarginAfter": "1000.00",
        "maintMarginBefore": "800.00",
        "maintMarginAfter": "800.00",
        "equityWithLoanBefore": "10000.00",
        "equityWithLoanAfter": "10000.00",
        "commission": 1.0,
        "maxCommission": None,
        "minCommission": None,
        "commissionCurrency": "USD",
        "warningText": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def default_summary(account: str = ACCOUNT):
    values = {
        "NetLiquidation": "10000",
        "TotalCashValue": "4000",
        "BuyingPower": "8000",
        "ExcessLiquidity": "9200",
        "FullExcessLiquidity": "9200",
        "AvailableFunds": "9000",
        "InitMarginReq": "1000",
        "MaintMarginReq": "800",
        "GrossPositionValue": "20",
        "Cushion": "0.92",
        "SMA": "5000",
        "EquityWithLoanValue": "10000",
        "RegTEquity": "10000",
        "RegTMargin": "1000",
        "Leverage": "1.2",
        "AccountType": "MARGIN",
    }
    return [tag(account, name, value) for name, value in values.items()]


def fig_order(**overrides) -> dict:
    payload = {
        "symbol": "FIG",
        "secType": "STK",
        "side": "SELL",
        "qty": "1",
        "limit": "9999",
        "tif": "DAY",
    }
    payload.update(overrides)
    return payload


def plain_contract(spec):
    return SimpleNamespace(
        symbol=spec.symbol,
        secType=spec.sec_type,
        exchange=spec.exchange,
        currency=spec.currency,
        conId=spec.con_id or 0,
        localSymbol=spec.symbol,
        multiplier="",
        lastTradeDateOrContractMonth=spec.expiry,
        strike=float(spec.strike) if spec.strike is not None else 0.0,
        right=spec.right,
    )


def plain_order(spec, account: str):
    whole = spec.qty == spec.qty.to_integral_value()
    return SimpleNamespace(
        action=spec.side,
        totalQuantity=int(spec.qty) if whole else float(spec.qty),
        lmtPrice=float(spec.limit),
        tif=spec.tif,
        orderType="LMT",
        account=account,
        transmit=True,
        orderRef="ibkr-gw-trade",
    )


class Script:
    def __init__(self) -> None:
        self.ibs = []
        self.placed = []
        self.what_ifs = []
        self.summary = default_summary()
        self.positions = [position(ACCOUNT, 4242, 1)]
        self.managed = [ACCOUNT]
        self.con_id = 4242
        self.local_symbol = "FIG"
        self.multiplier = "1"
        self.force_sec_type = ""
        self.what_if = what_if_state()
        self.fail_place = False
        self.fail_connect = False
        self.open_trades = []
        self.fills = []

    def factory(self):
        ib = FakeIB(self)
        self.ibs.append(ib)
        return ib

    def readers(self):
        return [ib for ib in self.ibs if getattr(ib, "readonly", None) is True]

    def traders(self):
        return [ib for ib in self.ibs if getattr(ib, "readonly", None) is False]


class FakeIB:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.calls = []
        self.client_id = None
        self.readonly = None

    def connect(self, host, port, *, clientId, timeout, readonly, account):
        self.calls.append(("connect", clientId, readonly, host, port, account))
        if self.script.fail_connect:
            raise ConnectionRefusedError("refused")
        self.client_id = clientId
        self.readonly = readonly
        self.host = host
        self.port = port
        self.account = account

    def disconnect(self):
        self.calls.append("disconnect")

    def managedAccounts(self):
        self.calls.append("managedAccounts")
        return list(self.script.managed)

    def accountSummary(self, account=""):
        self.calls.append(("accountSummary", account))
        return list(self.script.summary)

    def positions(self):
        self.calls.append("positions")
        return list(self.script.positions)

    def qualifyContracts(self, *contracts):
        self.calls.append("qualify")
        for contract in contracts:
            contract.conId = self.script.con_id
            contract.localSymbol = self.script.local_symbol or contract.symbol
            if self.script.multiplier:
                contract.multiplier = self.script.multiplier
            if self.script.force_sec_type:
                contract.secType = self.script.force_sec_type
        return contracts

    def whatIfOrder(self, contract, order):
        self.calls.append("whatIf")
        self.script.what_ifs.append(order)
        state = self.script.what_if
        if isinstance(state, list):
            return state.pop(0)
        return state

    def placeOrder(self, contract, order):
        if self.readonly:
            raise AssertionError("placeOrder on a read-only connection")
        self.calls.append("place")
        self.script.placed.append((self, order))
        if self.script.fail_place:
            raise RuntimeError("reset")
        return SimpleNamespace(
            order=SimpleNamespace(
                orderId=42,
                account=order.account,
                action=order.action,
                totalQuantity=order.totalQuantity,
                lmtPrice=order.lmtPrice,
                tif=order.tif,
                orderType=order.orderType,
            ),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=order.totalQuantity, avgFillPrice=0),
            contract=contract,
            fills=[],
        )

    def reqAllOpenOrders(self):
        self.calls.append("reqAllOpenOrders")

    def openTrades(self):
        self.calls.append("openTrades")
        return list(self.script.open_trades)

    def reqExecutions(self, filt):
        self.calls.append("reqExecutions")
        return list(self.script.fills)


class Runtime:
    def __init__(self, home) -> None:
        self.home = home
        self.clock = Clock()
        init_config(home)
        self.reload()
        self.script = Script()
        self.broker = IbBroker(
            self.settings,
            ib_factory=self.script.factory,
            contract_builder=plain_contract,
            order_builder=plain_order,
        )
        self.store = ConfirmStore(home / "confirms.json")
        self.audit = AuditLog(home / "audit.jsonl")

    def reload(self) -> None:
        self.settings = load_settings(self.home)
        if hasattr(self, "broker"):
            self.broker.settings = self.settings

    def set_enabled(self, value: bool) -> None:
        path = self.home / "config.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["enabled"] = value
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        self.reload()

    def preview(self, **overrides):
        return self.preview_payload(fig_order(**overrides))

    def preview_payload(self, payload):
        spec = parse_order_spec(payload)
        return preview_order(
            spec,
            settings=self.settings,
            broker=self.broker,
            store=self.store,
            audit=self.audit,
            now=self.clock,
        )

    def submit(self, confirm: str):
        return submit_order(
            confirm,
            settings=self.settings,
            broker=self.broker,
            store=self.store,
            audit=self.audit,
            now=self.clock,
        )
