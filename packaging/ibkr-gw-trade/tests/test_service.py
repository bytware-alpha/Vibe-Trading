"""Refusal gates through a fake IB. No live gateway calls."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fakes import ACCOUNT, position, tag, what_if_state

from ibkr_gw_trade.broker import default_contract_builder, default_ib_factory, default_order_builder
from ibkr_gw_trade.errors import TradeError
from ibkr_gw_trade.gates import parse_order_spec
from ibkr_gw_trade.service import format_report, read_orders, read_trades


def _codes(call):
    with pytest.raises(TradeError) as caught:
        call()
    return caught.value


def test_disabled_preview_withholds_hash_and_does_not_place(runtime):
    report = runtime.preview()
    text = format_report(report)
    assert report["decision"] == "withheld"
    assert report["reason"] == "trading_disabled"
    assert report["confirm"] is None
    assert "PREVIEW ONLY" in text
    assert "conId=4242" in text
    assert "localSymbol=FIG" in text
    assert "LIMIT 9999" in text
    assert "est commission 1" in text
    assert "init margin before 1000 after 1000" in text
    assert "maint margin before 800 after 800" in text
    assert "equity with loan before 10000 after 10000" in text
    assert "SMA 5000" in text
    assert "ExcessLiquidity 9200" in text
    assert "AvailableFunds 9000" in text
    assert "projected ExcessLiquidity 9200" in text
    assert "projected AvailableFunds 9000" in text
    assert "confirm withheld" in text
    assert runtime.script.placed == []
    assert runtime.script.what_ifs
    assert not (runtime.home / "confirms.json").exists()
    audit = (runtime.home / "audit.jsonl").read_text(encoding="utf-8")
    assert "withheld" in audit or "trading_disabled" in audit
    assert "4242" in audit


def test_enabled_submit_uses_trading_client_and_limit_order(runtime):
    runtime.set_enabled(True)
    report = runtime.preview()
    placed = runtime.submit(report["confirm"])
    assert placed["decision"] == "placed"
    assert placed["orderId"] == "42"
    assert placed["status"] == "Submitted"
    assert len(runtime.script.placed) == 1
    ib, order = runtime.script.placed[0]
    assert ib.client_id == 18
    assert ib.readonly is False
    assert order.action == "SELL"
    assert order.totalQuantity == 1
    assert order.lmtPrice == 9999
    assert order.tif == "DAY"
    assert order.orderType == "LMT"
    assert order.account == ACCOUNT
    assert order.transmit is True
    assert runtime.script.readers()
    assert all(reader.client_id == 17 and reader.readonly is True for reader in runtime.script.readers())
    assert all("whatIf" not in reader.calls and "place" not in reader.calls for reader in runtime.script.readers())
    assert all("accountSummary" not in trader.calls for trader in runtime.script.traders())
    audit = (runtime.home / "audit.jsonl").read_text(encoding="utf-8")
    assert report["confirm"] not in audit
    assert report["confirm"][:8] in audit
    assert (runtime.home / "confirms.json").stat().st_mode & 0o777 == 0o600
    assert (runtime.home / "audit.jsonl").stat().st_mode & 0o777 == 0o600


def test_disabled_submit_does_not_consume_the_hash(runtime):
    runtime.set_enabled(True)
    report = runtime.preview()
    runtime.set_enabled(False)
    exc = _codes(lambda: runtime.submit(report["confirm"]))
    assert exc.code == "trading_disabled"
    assert runtime.script.placed == []
    runtime.set_enabled(True)
    placed = runtime.submit(report["confirm"])
    assert placed["orderId"] == "42"
    assert len(runtime.script.placed) == 1


def test_hash_reuse_expiry_and_supersede(runtime):
    runtime.set_enabled(True)
    first = runtime.preview()
    runtime.clock.advance(120)
    expired = _codes(lambda: runtime.submit(first["confirm"]))
    assert expired.code == "hash_expired"
    assert runtime.script.placed == []
    what_ifs_after_expiry = len(runtime.script.what_ifs)

    runtime.clock.advance(-120)
    second = runtime.preview()
    runtime.clock.advance(119)
    runtime.submit(second["confirm"])
    reused = _codes(lambda: runtime.submit(second["confirm"]))
    assert reused.code == "hash_used"
    assert len(runtime.script.placed) == 1

    third = runtime.preview()
    fourth = runtime.preview()
    superseded = _codes(lambda: runtime.submit(third["confirm"]))
    assert superseded.code == "hash_superseded"
    runtime.submit(fourth["confirm"])
    assert len(runtime.script.placed) == 2
    assert len(runtime.script.what_ifs) > what_ifs_after_expiry


def test_changed_what_if_refuses_before_place(runtime):
    runtime.set_enabled(True)
    original = what_if_state()
    changed = what_if_state(commission=2)
    runtime.script.what_if = [original, changed]
    report = runtime.preview()
    exc = _codes(lambda: runtime.submit(report["confirm"]))
    assert exc.code == "confirm_mismatch"
    assert "commission" in str(exc)
    assert runtime.script.placed == []
    record = json.loads((runtime.home / "confirms.json").read_text(encoding="utf-8"))
    assert record["confirms"][report["confirm"]]["used"] is False


def test_place_failure_consumes_the_hash(runtime):
    runtime.set_enabled(True)
    report = runtime.preview()
    runtime.script.fail_place = True
    exc = _codes(lambda: runtime.submit(report["confirm"]))
    assert exc.code == "place_failed"
    assert "consumed" in str(exc)
    assert len(runtime.script.placed) == 1
    runtime.script.fail_place = False
    again = _codes(lambda: runtime.submit(report["confirm"]))
    assert again.code == "hash_used"
    assert len(runtime.script.placed) == 1


def test_sma_missing_unreadable_buffer_and_foreign_sma(runtime):
    runtime.script.summary = [row for row in runtime.script.summary if row.tag != "SMA"]
    missing = _codes(lambda: runtime.preview(side="BUY", qty="1", limit="10"))
    assert missing.code == "sma_missing"
    assert runtime.script.what_ifs
    assert runtime.script.placed == []

    runtime.script.summary.append(tag(ACCOUNT, "SMA", "N/A"))
    unreadable = _codes(lambda: runtime.preview(side="BUY", qty="1", limit="10"))
    assert unreadable.code == "sma_unreadable"

    runtime.script.summary = [row for row in runtime.script.summary if row.tag != "SMA"]
    runtime.script.summary.append(tag(ACCOUNT, "SMA", "1000"))
    buffered = _codes(lambda: runtime.preview(side="BUY", qty="10", limit="100"))
    assert buffered.code == "sma_buffer"
    assert "500" in str(buffered) or "buffer" in str(buffered)

    runtime.script.summary = [row for row in runtime.script.summary if row.tag != "SMA"]
    runtime.script.summary.append(tag("DU9999999", "SMA", "999999"))
    foreign = _codes(lambda: runtime.preview(side="BUY", qty="1", limit="10"))
    assert foreign.code == "sma_missing"
    assert runtime.script.placed == []


def test_exact_sma_buffer_is_allowed(runtime):
    runtime.set_enabled(True)
    runtime.script.summary = [row for row in runtime.script.summary if row.tag != "SMA"]
    runtime.script.summary.append(tag(ACCOUNT, "SMA", "1500"))
    runtime.script.positions = []
    report = runtime.preview(side="BUY", qty="10", limit="100")
    assert report["snapshot"]["projectedSma"] == "1000"
    assert report["snapshot"]["smaImpact"] == "-500"
    assert report["confirm"]


def test_liquidity_refusals_and_zero_is_allowed(runtime):
    runtime.script.what_if = what_if_state(maintMarginAfter="11000")
    el = _codes(runtime.preview)
    assert el.code == "excess_liquidity"
    assert runtime.script.placed == []

    runtime.script.what_if = what_if_state(initMarginAfter="11000")
    af = _codes(runtime.preview)
    assert af.code == "available_funds"

    runtime.script.what_if = what_if_state(equityWithLoanAfter="800", maintMarginAfter="800", initMarginAfter="100")
    runtime.set_enabled(True)
    report = runtime.preview()
    assert report["snapshot"]["projectedExcessLiquidity"] == "0"
    assert report["confirm"]

    runtime.script.what_if = what_if_state(commission=1.7976931348623157e308)
    incomplete = _codes(runtime.preview)
    assert incomplete.code == "what_if_incomplete"
    assert runtime.script.placed == []


def test_close_qty_short_naked_and_cover_do_not_open_a_trading_connection(runtime):
    runtime.script.positions = [position(ACCOUNT, 4242, 1)]
    too_many = _codes(lambda: runtime.preview(qty="2"))
    assert too_many.code == "close_qty"
    assert runtime.script.traders() == []
    assert runtime.script.what_ifs == []

    runtime.script.positions = []
    naked = _codes(runtime.preview)
    assert naked.code == "close_qty"
    assert runtime.script.traders() == []

    runtime.script.positions = [position(ACCOUNT, 4242, -1)]
    short = _codes(runtime.preview)
    assert short.code == "close_qty"
    cover = _codes(lambda: runtime.preview(side="BUY", qty="1", limit="10"))
    assert cover.code == "cover_unsupported"
    assert runtime.script.traders() == []
    assert runtime.script.placed == []

    runtime.script.positions = [position("DU9999999", 4242, 5)]
    foreign = _codes(runtime.preview)
    assert foreign.code == "close_qty"


def test_option_sma_and_stock_multiplier_is_ignored(runtime):
    runtime.script.multiplier = "100"
    runtime.script.positions = []
    stock = runtime.preview(side="BUY", qty="1", limit="100")
    assert stock["snapshot"]["smaImpact"] == "-50"
    assert stock["snapshot"]["multiplier"] == "1"

    runtime.script.local_symbol = "AMD   270128C00350000"
    runtime.script.positions = []
    opened = runtime.preview(
        symbol="AMD",
        secType="OPT",
        side="BUY",
        qty="1",
        limit="2.50",
        expiry="20270128",
        strike="350",
        right="C",
    )
    assert opened["snapshot"]["smaImpact"] == "-250"
    assert opened["snapshot"]["multiplier"] == "100"
    assert opened["snapshot"]["localSymbol"] == "AMD   270128C00350000"

    runtime.script.positions = [position(ACCOUNT, 4242, 1, symbol="AMD", sec="OPT")]
    closed = runtime.preview(
        symbol="AMD",
        secType="OPT",
        side="SELL",
        qty="1",
        limit="2.50",
        expiry="20270128",
        strike="350",
        right="C",
    )
    assert closed["snapshot"]["smaImpact"] == "250"

    runtime.script.positions = []
    naked = _codes(
        lambda: runtime.preview(
            symbol="AMD",
            secType="OPT",
            side="SELL",
            qty="1",
            limit="2.50",
            expiry="20270128",
            strike="350",
            right="P",
        )
    )
    assert naked.code == "close_qty"


def test_shape_refusals_do_not_connect(runtime):
    market = _codes(lambda: runtime.preview_payload({**runtime_order(), "orderType": "MKT"}))
    stop = _codes(lambda: runtime.preview_payload({**runtime_order(), "orderType": "STP"}))
    bad_tif = _codes(lambda: runtime.preview_payload({**runtime_order(), "tif": "IOC"}))
    future = _codes(lambda: runtime.preview_payload({**runtime_order(), "secType": "FUT"}))
    assert market.code == "not_limit"
    assert stop.code == "not_limit"
    assert bad_tif.code == "bad_tif"
    assert future.code == "bad_sec_type"
    assert runtime.script.ibs == []
    gtc = runtime.preview(tif="GTC")
    assert gtc["snapshot"]["tif"] == "GTC"


def test_account_visibility_and_ambiguous_tag(runtime):
    runtime.script.managed = ["DU1234567"]
    hidden = _codes(runtime.preview)
    assert hidden.code == "account_not_visible"
    assert runtime.script.traders() == []
    assert runtime.script.placed == []

    runtime.script.managed = [ACCOUNT]
    runtime.script.summary.append(tag(ACCOUNT, "SMA", "1"))
    ambiguous = _codes(lambda: runtime.preview(side="BUY", qty="1", limit="10"))
    assert ambiguous.code == "ambiguous_tag"
    assert runtime.script.placed == []


def test_unknown_hash_does_not_connect_when_trading_is_enabled(runtime):
    runtime.set_enabled(True)
    exc = _codes(lambda: runtime.submit("ab" * 32))
    assert exc.code == "hash_unknown"
    assert runtime.script.ibs == []


def test_orders_and_todays_fills_are_audited(runtime):
    day = runtime.clock.day
    runtime.script.open_trades = [
        _trade(order_id=7, account=ACCOUNT),
        _trade(order_id=8, account="DU9999999"),
    ]
    yesterday = day - timedelta(days=1)
    runtime.script.fills = [
        _fill("exec-today", day, ACCOUNT),
        _fill("exec-old", yesterday, ACCOUNT),
        _fill("exec-undated", None, ACCOUNT),
        _fill("exec-other", day, "DU9999999"),
    ]
    orders = read_orders(settings=runtime.settings, broker=runtime.broker, audit=runtime.audit, now=runtime.clock)
    trades = read_trades(settings=runtime.settings, broker=runtime.broker, audit=runtime.audit, now=runtime.clock)
    assert [row["orderId"] for row in orders["orders"]] == ["7"]
    assert [row["execId"] for row in trades["trades"]] == ["exec-today"]
    assert trades["skippedUndated"] == 1
    audit = (runtime.home / "audit.jsonl").read_text(encoding="utf-8")
    assert "exec-today" in audit
    assert "exec-old" not in audit
    assert '"orderId":"7"' in audit
    assert all(ib.readonly is True and ib.client_id == 17 for ib in runtime.script.ibs)


def test_reader_cannot_place(runtime):
    spec = parse_order_spec(runtime_order())
    with runtime.broker.exclusive():
        reader = runtime.broker.connect(trading=False)
        with pytest.raises(TradeError) as exc:
            reader.place(object(), spec)
        assert exc.value.code == "readonly_path"
        reader.disconnect()
    assert runtime.script.placed == []


def test_default_builders_do_not_connect(monkeypatch):
    calls = []

    class FakeModule:
        class IB:
            def connect(self, *args, **kwargs):
                calls.append("connect")

        @staticmethod
        def LimitOrder(action, qty, price, **kwargs):
            order = type("Order", (), {})()
            order.orderType = "LMT"
            order.action = action
            order.totalQuantity = qty
            order.lmtPrice = price
            for key, value in kwargs.items():
                setattr(order, key, value)
            return order

    monkeypatch.setitem(__import__("sys").modules, "ib_async", FakeModule)
    client = default_ib_factory()
    assert calls == []
    assert isinstance(client, FakeModule.IB)
    spec = parse_order_spec(runtime_order())
    order = default_order_builder(spec, ACCOUNT)
    assert order.orderType == "LMT"
    assert order.action == "SELL"
    assert order.tif == "DAY"
    assert order.account == ACCOUNT
    assert order.transmit is True
    assert calls == []


def test_real_ib_async_builders_do_not_connect():
    pytest.importorskip("ib_async")
    spec = parse_order_spec(runtime_order())
    order = default_order_builder(spec, ACCOUNT)
    assert order.orderType == "LMT"
    assert order.action == "SELL"
    assert order.totalQuantity == 1
    assert order.lmtPrice == 9999
    assert order.tif == "DAY"
    assert order.account == ACCOUNT
    assert order.transmit is True
    stock = default_contract_builder(spec)
    assert stock.symbol == "FIG"
    assert stock.secType == "STK"
    option = default_contract_builder(
        parse_order_spec(
            {
                "symbol": "AMD",
                "secType": "OPT",
                "side": "SELL",
                "qty": "1",
                "limit": "0.05",
                "tif": "DAY",
                "expiry": "20270128",
                "strike": "350",
                "right": "C",
            }
        )
    )
    assert option.secType == "OPT"
    assert option.lastTradeDateOrContractMonth == "20270128"
    assert option.right == "C"
    client = default_ib_factory()
    assert client.isConnected() is False


def test_audit_refuses_a_planted_confirm_hash(runtime):
    digest = "ab" * 32
    runtime.audit.forbid(digest)
    with pytest.raises(TradeError) as exc:
        runtime.audit.write({"event": "preview", "warning": digest}, runtime.clock())
    assert exc.value.code == "audit_blocked"
    assert not (runtime.home / "audit.jsonl").exists()


def runtime_order():
    return {
        "symbol": "FIG",
        "secType": "STK",
        "side": "SELL",
        "qty": "1",
        "limit": "9999",
        "tif": "DAY",
    }


def _trade(order_id: int, account: str):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=4242, localSymbol="FIG", symbol="FIG", secType="STK"),
        order=SimpleNamespace(
            orderId=order_id,
            account=account,
            action="SELL",
            totalQuantity=1,
            lmtPrice=9999,
            tif="DAY",
            orderType="LMT",
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1, avgFillPrice=0),
    )


def _fill(exec_id: str, day, account: str):
    when = datetime(day.year, day.month, day.day, 15, 0, 0) if day else None
    return SimpleNamespace(
        time=when,
        contract=SimpleNamespace(conId=4242, localSymbol="FIG", symbol="FIG", secType="STK"),
        execution=SimpleNamespace(
            execId=exec_id,
            orderId=7,
            acctNumber=account,
            side="SLD",
            shares=1,
            price=10.5,
            time="",
        ),
    )
