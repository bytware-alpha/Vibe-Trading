"""IBKR official MCP mapping for generic read-only trading operations."""

from __future__ import annotations

from typing import Any

from src.trading.connectors.ibkr.official_mcp_catalog import (
    readonly_remote_tools as catalog_readonly_remote_tools,
)

_REMOTE_TOOL_NAMES = {
    "account": "get_account_summary",
    "positions": "get_account_positions",
    "orders": "get_account_orders",
}

_ACCOUNT_TAGS = {
    "net_liquidation": "NetLiquidation",
    "equity_with_loan_value": "EquityWithLoanValue",
    "buying_power": "BuyingPower",
    "gross_position_value": "GrossPositionValue",
    "total_cash_value": "TotalCashValue",
    "available_funds": "AvailableFunds",
    "initial_margin": "InitMarginReq",
    "maintenance_margin": "MaintMarginReq",
    "excess_liquidity": "ExcessLiquidity",
    "dividends": "AccruedCash",
    "leverage": "Leverage",
}


def readonly_remote_tools() -> frozenset[str]:
    """Return the fail-closed IBKR official MCP read allowlist."""
    return catalog_readonly_remote_tools()


def remote_tool_name(operation: str) -> str | None:
    """Return the verified IBKR public MCP tool for a generic operation."""
    return _REMOTE_TOOL_NAMES.get(operation)


def remote_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map generic trading arguments onto IBKR official MCP payloads."""
    if operation == "account":
        return {}
    if operation == "positions":
        return {}
    if operation == "orders":
        if arguments.get("include_executions") or arguments.get("show_completed"):
            return {"show_completed": True}
        return {}
    return dict(arguments)


def normalize_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    """Translate IBKR's structured MCP result into the shared broker shapes."""
    normalized = dict(result)
    structured = result.get("structured_content")
    if not isinstance(structured, dict):
        return normalized

    if operation == "account":
        currency = str(structured.get("currency") or "USD").upper()
        normalized["summary"] = [
            {"tag": tag, "value": structured.get(key), "currency": currency}
            for key, tag in _ACCOUNT_TAGS.items()
            if structured.get(key) is not None
        ]
        return normalized

    if operation == "positions":
        rows = structured.get("positions")
        if not isinstance(rows, list):
            return normalized
        normalized["positions"] = [
            {
                "contract_id": row.get("contract_id"),
                "symbol": row.get("contract_description"),
                "position": row.get("position"),
                "market_price": row.get("market_price"),
                "market_value": row.get("market_value"),
                "currency": row.get("currency"),
                "avg_cost": row.get("average_price"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "sec_type": row.get("asset_class"),
            }
            for row in rows
            if isinstance(row, dict)
        ]
        return normalized

    if operation == "orders":
        rows = structured.get("orders")
        if not isinstance(rows, list):
            return normalized
        normalized["open_orders"] = [
            {
                "order_id": row.get("order_id"),
                "symbol": row.get("primary_description") or row.get("secondary_description"),
                "action": row.get("side"),
                "order": {
                    "orderType": row.get("order_type"),
                    "totalQuantity": row.get("total_shares_qty"),
                    "lmtPrice": row.get("limit_price"),
                },
                "status": {"status": row.get("order_status")},
            }
            for row in rows
            if isinstance(row, dict)
        ]
    return normalized
