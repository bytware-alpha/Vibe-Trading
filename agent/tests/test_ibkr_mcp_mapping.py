"""Tests for the verified IBKR public MCP generic read mapping."""

from __future__ import annotations

import pytest

from src.trading.connectors.ibkr.mcp import (
    normalize_result,
    readonly_remote_tools,
    remote_arguments,
    remote_tool_name,
)
from src.trading.connectors.ibkr.official_mcp_catalog import (
    ACCOUNT_INVENTORY_READS,
    EXCLUDED_READONLY_REMOTE_TOOLS,
)

pytestmark = pytest.mark.unit


def test_ibkr_remote_tools_map_verified_reads() -> None:
    assert remote_tool_name("account") == "get_account_summary"
    assert remote_tool_name("positions") == "get_account_positions"
    assert remote_tool_name("orders") == "get_account_orders"
    assert remote_tool_name("place_order") is None
    allowed = readonly_remote_tools()
    assert {"get_account_summary", "get_account_positions", "get_account_balances"} <= allowed
    assert "submit_order" not in allowed
    assert "preview_order" not in allowed
    assert remote_arguments("account", {"account_number": "ignored"}) == {}
    assert remote_arguments("orders", {"include_executions": True}) == {"show_completed": True}
    assert remote_arguments("orders", {}) == {}


def test_ibkr_account_summary_normalizes_for_portfolio_totals() -> None:
    result = normalize_result(
        "account",
        {
            "status": "ok",
            "structured_content": {
                "currency": "USD",
                "net_liquidation": 123.45,
                "total_cash_value": 12.34,
            },
        },
    )

    assert result["summary"] == [
        {"tag": "NetLiquidation", "value": 123.45, "currency": "USD"},
        {"tag": "TotalCashValue", "value": 12.34, "currency": "USD"},
    ]


def test_ibkr_positions_normalize_for_shared_position_reader() -> None:
    result = normalize_result(
        "positions",
        {
            "status": "ok",
            "structured_content": {
                "positions": [
                    {
                        "contract_id": 1,
                        "contract_description": "EXAMPLE",
                        "position": 2.0,
                        "market_price": 10.0,
                        "market_value": 20.0,
                        "currency": "USD",
                        "average_price": 8.0,
                        "unrealized_pnl": 4.0,
                        "asset_class": "STK",
                    }
                ]
            },
        },
    )

    assert result["positions"] == [
        {
            "contract_id": 1,
            "symbol": "EXAMPLE",
            "position": 2.0,
            "market_price": 10.0,
            "market_value": 20.0,
            "currency": "USD",
            "avg_cost": 8.0,
            "unrealized_pnl": 4.0,
            "sec_type": "STK",
        }
    ]


def test_ibkr_orders_normalize_for_shared_order_reader() -> None:
    result = normalize_result(
        "orders",
        {
            "status": "ok",
            "structured_content": {
                "orders": [
                    {
                        "order_id": 9,
                        "primary_description": "EXAMPLE",
                        "side": "BUY",
                        "order_type": "LMT",
                        "total_shares_qty": "10",
                        "limit_price": "1.25",
                        "order_status": "Submitted",
                    }
                ]
            },
        },
    )

    assert result["open_orders"] == [
        {
            "order_id": 9,
            "symbol": "EXAMPLE",
            "action": "BUY",
            "order": {"orderType": "LMT", "totalQuantity": "10", "lmtPrice": "1.25"},
            "status": {"status": "Submitted"},
        }
    ]


def test_account_inventory_reads_are_allowlisted() -> None:
    allowed = readonly_remote_tools()
    for name, _arguments in ACCOUNT_INVENTORY_READS:
        assert name in allowed
        assert name not in EXCLUDED_READONLY_REMOTE_TOOLS
