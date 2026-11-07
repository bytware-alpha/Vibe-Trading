"""Fail-closed catalog of IBKR official MCP tools from a live tools/list dump.

The JSON next to this module is a credential-free snapshot of the official
server's ``tools/list`` payload. Adapter allowlisting, argument checks, and
the account-inventory command read from it so an unannotated or newly named
tool stays denied until it is classified here.

Read-only allowlisting is annotation-first and then subtracts the excluded
set. IBKR marks some order-construction helpers ``readOnlyHint: true``
(``preview_order``, ``preview_screener``, ``get_combo_identifier``); those
stay blocked because they are not account/market inventory reads.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_INVENTORY_PATH = Path(__file__).with_name("official_mcp_tools.json")

#: Annotated read-only tools that are still refused. ``preview_order`` is an
#: order what-if tied to the submit sequence; ``preview_screener`` runs an
#: unsaved screener; ``get_combo_identifier`` builds combo ids for
#: ``create_order_instruction``.
EXCLUDED_READONLY_REMOTE_TOOLS: frozenset[str] = frozenset(
    {
        "preview_order",
        "preview_screener",
        "get_combo_identifier",
    }
)

#: Schema-supported account inventory reads. These need no user-supplied id.
#: ``get_account_trades.period`` and ``get_account_orders.show_completed`` use
#: the documented enum / boolean; ``get_pa_allocation.type`` uses ``ALL``.
ACCOUNT_INVENTORY_READS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("get_flow_type", {}),
    ("get_account_summary", {}),
    ("get_account_balances", {}),
    ("get_account_positions", {}),
    ("get_account_orders", {"show_completed": True}),
    ("get_account_trades", {"period": "DAYS_7"}),
    ("get_pa_performance_all_periods", {}),
    ("get_pa_allocation", {"type": "ALL"}),
    ("get_watchlists", {}),
    ("get_alerts", {}),
    ("list_saved_screeners", {}),
    ("get_order_instructions", {}),
    ("get_screener_asset_classes", {}),
    ("whats_new", {}),
)


@lru_cache(maxsize=1)
def load_official_mcp_tools() -> tuple[dict[str, Any], ...]:
    """Return the vendored official IBKR MCP ``tools/list`` snapshot."""
    payload = json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("official IBKR MCP inventory must be a JSON array")
    tools: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("official IBKR MCP inventory entries must be named objects")
        tools.append(item)
    return tuple(tools)


def official_mcp_tool_by_name() -> dict[str, dict[str, Any]]:
    """Return inventory tools keyed by remote name."""
    return {str(tool["name"]): tool for tool in load_official_mcp_tools()}


def _annotation_flag(tool: Mapping[str, Any], name: str) -> Any:
    annotations = tool.get("annotations")
    if not isinstance(annotations, Mapping):
        return None
    return annotations.get(name)


def annotated_readonly_remote_tools() -> frozenset[str]:
    """Return names IBKR annotated ``readOnlyHint: true`` and not destructive."""
    names: set[str] = set()
    for tool in load_official_mcp_tools():
        if _annotation_flag(tool, "readOnlyHint") is not True:
            continue
        if _annotation_flag(tool, "destructiveHint") is True:
            continue
        names.add(str(tool["name"]))
    return frozenset(names)


def mutating_remote_tools() -> frozenset[str]:
    """Return names IBKR annotated as not read-only and/or destructive."""
    names: set[str] = set()
    for tool in load_official_mcp_tools():
        readonly = _annotation_flag(tool, "readOnlyHint")
        destructive = _annotation_flag(tool, "destructiveHint")
        if readonly is True and destructive is not True:
            continue
        names.add(str(tool["name"]))
    return frozenset(names)


def readonly_remote_tools() -> frozenset[str]:
    """Return the fail-closed IBKR official MCP read allowlist."""
    return annotated_readonly_remote_tools() - EXCLUDED_READONLY_REMOTE_TOOLS


def is_ibkr_readonly_remote_tool(remote_name: str) -> bool:
    """Return whether ``remote_name`` is on the fail-closed read allowlist."""
    return remote_name in readonly_remote_tools()


def _schema_properties(tool: Mapping[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return dict(properties)


def _schema_required(tool: Mapping[str, Any]) -> tuple[str, ...]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, Mapping):
        return ()
    required = schema.get("required") or []
    if not isinstance(required, list):
        return ()
    return tuple(str(name) for name in required)


def validate_readonly_arguments(remote_name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``arguments`` after fail-closed schema checks.

    Unknown properties, missing required fields, and enum mismatches are
    refused. Extra IBKR response fields are not modeled here.

    Args:
        remote_name: Allowlisted remote tool name.
        arguments: Caller-supplied JSON object, or ``None``.

    Returns:
        The validated argument mapping (possibly empty).

    Raises:
        ValueError: If the name is unknown, required fields are missing, an
            argument is not in the schema, or an enum value is invalid.
    """
    tool = official_mcp_tool_by_name().get(remote_name)
    if tool is None:
        raise ValueError(f"unknown IBKR official MCP tool '{remote_name}'")
    payload = dict(arguments or {})
    properties = _schema_properties(tool)
    unknown = sorted(key for key in payload if key not in properties)
    if unknown:
        raise ValueError(
            f"IBKR tool '{remote_name}' rejected unknown argument(s) {unknown}"
        )
    missing = [name for name in _schema_required(tool) if name not in payload]
    if missing:
        raise ValueError(
            f"IBKR tool '{remote_name}' missing required argument(s) {missing}"
        )
    for key, value in payload.items():
        spec = properties.get(key)
        if not isinstance(spec, Mapping):
            continue
        allowed = spec.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            raise ValueError(
                f"IBKR tool '{remote_name}' argument '{key}' must be one of {allowed}"
            )
    return payload
