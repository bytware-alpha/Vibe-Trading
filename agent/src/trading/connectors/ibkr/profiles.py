"""Built-in Interactive Brokers connector profiles."""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

IBKR_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="ibkr-paper-local",
        connector="ibkr",
        label="IBKR Paper · TWS / Gateway",
        environment="paper",
        transport="local_tws",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 77},
        notes="Uses the user's local TWS paper session. No IBKR credentials enter Vibe-Trading.",
    ),
    TradingProfile(
        id="ibkr-live-local-readonly",
        connector="ibkr",
        label="IBKR Live · TWS / Gateway Read-Only",
        environment="live",
        transport="local_tws",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly", "host": "127.0.0.1", "port": 7496, "client_id": 77},
        notes="Reads a local live TWS/Gateway session only. Order placement is not exposed.",
    ),
    TradingProfile(
        id="ibkr-live-official-mcp-readonly",
        connector="ibkr",
        label="IBKR Live · Official MCP Read-Only",
        environment="live",
        transport="remote_mcp",
        capabilities=("account.read", "positions.read", "orders.read"),
        readonly=True,
        config={"server": "ibkr"},
        notes=(
            "Requires IBKR official MCP OAuth approval. Fail-closed read "
            "allowlist from the official tools/list; mutations stay blocked. "
            "Use connector inventory / trading_inventory and connector read "
            "/ trading_read for the remaining annotated reads."
        ),
    ),
    TradingProfile(
        id="ibkr-live-official-mcp-trading",
        connector="ibkr",
        label="IBKR Live · Official MCP Trading Capability",
        environment="live",
        transport="remote_mcp",
        capabilities=("account.read", "positions.read", "orders.read"),
        readonly=True,
        config={"server": "ibkr"},
        notes=(
            "Requires IBKR official MCP OAuth for documented scopes "
            "mcp.read, mcp.write, and mcp.orders.submit. Authorizes trading "
            "capability on the connection only; this profile does not expose "
            "order placement through vibe-trading-mcp or trading_* tools. "
            "Read allowlist and inventory/raw-read are the same as the "
            "read-only profile."
        ),
    ),
)
