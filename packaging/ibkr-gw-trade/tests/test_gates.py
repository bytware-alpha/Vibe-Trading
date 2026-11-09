"""Pure gate math. No gateway object."""

from decimal import Decimal

import pytest
from fakes import fig_order

from ibkr_gw_trade.errors import TradeError
from ibkr_gw_trade.gates import parse_order_spec, position_gate, risk_gate, sma_impact
from ibkr_gw_trade.money import held_limit, parse_money


@pytest.mark.parametrize(
    ("sec", "side", "qty", "limit", "mult", "expected"),
    [
        ("OPT", "BUY", "1", "2.50", "100", Decimal(-250)),
        ("STK", "BUY", "10", "20", "1", Decimal(-100)),
        ("OPT", "SELL", "1", "2.50", "100", Decimal(250)),
        ("STK", "SELL", "2", "10", "1", Decimal(10)),
        ("STK", "BUY", "1", "9999", "100", Decimal("-4999.50")),
        ("STK", "SELL", "1", "9999", "1", Decimal("4999.50")),
    ],
)
def test_sma_impact(sec, side, qty, limit, mult, expected):
    spec = parse_order_spec(
        {
            "symbol": "FIG" if sec == "STK" else "AMD",
            "secType": sec,
            "side": side,
            "qty": qty,
            "limit": limit,
            "tif": "DAY",
            **({} if sec == "STK" else {"expiry": "20270128", "strike": "350", "right": "C"}),
        }
    )
    impact, reason = sma_impact(spec, Decimal(mult))
    assert impact == expected
    if sec == "OPT" and side == "BUY":
        assert "100% of debit" in reason
    if sec == "STK" and side == "BUY":
        assert "50% of value" in reason
    if sec == "OPT" and side == "SELL":
        assert "100% of credit" in reason
    if sec == "STK" and side == "SELL":
        assert "50% of value" in reason
    if sec == "STK":
        assert "x multiplier" not in reason


def test_sma_buffer_boundary():
    allowed = risk_gate(
        side="BUY",
        sma=Decimal(1500),
        sma_status="ok",
        impact=Decimal(-500),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    refused = risk_gate(
        side="BUY",
        sma=Decimal("1499.99"),
        sma_status="ok",
        impact=Decimal(-500),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    assert allowed.ok
    assert refused.code == "sma_buffer"


def test_missing_and_unreadable_sma_block_buys_only():
    missing = risk_gate(
        side="BUY",
        sma=None,
        sma_status="missing",
        impact=Decimal(-1),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    unreadable = risk_gate(
        side="BUY",
        sma=None,
        sma_status="unreadable",
        impact=Decimal(-1),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    close = risk_gate(
        side="SELL",
        sma=None,
        sma_status="missing",
        impact=Decimal(10),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    underwater = risk_gate(
        side="SELL",
        sma=Decimal(-100),
        sma_status="ok",
        impact=Decimal(10),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal(1),
    )
    assert missing.code == "sma_missing"
    assert unreadable.code == "sma_unreadable"
    assert close.ok
    assert underwater.ok


def test_liquidity_gates():
    negative_el = risk_gate(
        side="SELL",
        sma=Decimal(5000),
        sma_status="ok",
        impact=Decimal(1),
        buffer=Decimal(1000),
        projected_el=Decimal("-0.01"),
        projected_af=Decimal(1),
    )
    zero = risk_gate(
        side="SELL",
        sma=Decimal(5000),
        sma_status="ok",
        impact=Decimal(1),
        buffer=Decimal(1000),
        projected_el=Decimal(0),
        projected_af=Decimal(0),
    )
    negative_af = risk_gate(
        side="SELL",
        sma=Decimal(5000),
        sma_status="ok",
        impact=Decimal(1),
        buffer=Decimal(1000),
        projected_el=Decimal(1),
        projected_af=Decimal("-0.01"),
    )
    both = risk_gate(
        side="BUY",
        sma=None,
        sma_status="missing",
        impact=Decimal(-1),
        buffer=Decimal(1000),
        projected_el=Decimal(-1),
        projected_af=Decimal(-1),
    )
    assert negative_el.code == "excess_liquidity"
    assert zero.ok
    assert negative_af.code == "available_funds"
    assert both.code == "excess_liquidity"


@pytest.mark.parametrize(
    ("side", "qty", "held", "code"),
    [
        ("SELL", "1", "1", None),
        ("SELL", "1.0001", "1", "close_qty"),
        ("SELL", "1", "0", "close_qty"),
        ("SELL", "1", "-1", "close_qty"),
        ("BUY", "1", "-1", "cover_unsupported"),
        ("BUY", "1", "0", None),
        ("SELL", "0.5", "0.5", None),
        ("SELL", "0.6", "0.5", "close_qty"),
    ],
)
def test_close_qty(side, qty, held, code):
    spec = parse_order_spec(fig_order(side=side, qty=qty))
    result = position_gate(spec, Decimal(held))
    assert result.code == code


def test_held_limit_never_rounds_a_long_up_or_a_short_toward_zero():
    assert held_limit(Decimal(1)) == Decimal(1)
    assert held_limit(Decimal("1.00019")) == Decimal("1.0001")
    assert held_limit(Decimal("-0.00001")) == Decimal("-0.0001")
    assert held_limit(Decimal(0)) == Decimal(0)


def test_money_parser():
    assert parse_money("(50.00)") == Decimal("-50.00")
    assert parse_money("1,234.50 USD") == Decimal("1234.50")
    assert parse_money(1.7976931348623157e308) is None
    assert parse_money("") is None
    assert parse_money("0") == Decimal(0)


def test_shape_refusals_have_no_broker():
    with pytest.raises(TradeError) as market:
        parse_order_spec(fig_order(orderType="MKT"))
    with pytest.raises(TradeError) as stop:
        parse_order_spec(fig_order(orderType="STP"))
    with pytest.raises(TradeError) as stop_limit:
        parse_order_spec(fig_order(orderType="STP LMT"))
    with pytest.raises(TradeError) as tif:
        parse_order_spec(fig_order(tif="IOC"))
    with pytest.raises(TradeError) as sec:
        parse_order_spec(fig_order(secType="FUT"))
    assert market.value.code == "not_limit"
    assert stop.value.code == "not_limit"
    assert stop_limit.value.code == "not_limit"
    assert tif.value.code == "bad_tif"
    assert sec.value.code == "bad_sec_type"
    assert parse_order_spec(fig_order(tif="gtc")).tif == "GTC"
    assert parse_order_spec(fig_order(orderType="LIMIT")).side == "SELL"
