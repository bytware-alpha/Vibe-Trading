"""Curated IBKR MCP tool classification overrides.

The official catalog is vendored from a live ``tools/list``. Names IBKR
annotates as mutating, plus the excluded "read-only" order helpers, are pinned
WRITE so annotations cannot un-gate them. Unknown names still fail closed.
"""

from __future__ import annotations

from src.live.classification import ToolClass
from src.trading.connectors.ibkr.official_mcp_catalog import (
    EXCLUDED_READONLY_REMOTE_TOOLS,
    mutating_remote_tools,
)

_LEGACY_WRITE_ALIASES = (
    "place_order",
    "placeOrder",
    "submitOrder",
    "cancelOrder",
    "modifyOrder",
    "replace_order",
    "replaceOrder",
)

IBKR_TOOL_CLASS: dict[str, ToolClass] = {
    name: ToolClass.WRITE
    for name in (
        set(mutating_remote_tools())
        | set(EXCLUDED_READONLY_REMOTE_TOOLS)
        | set(_LEGACY_WRITE_ALIASES)
    )
}
