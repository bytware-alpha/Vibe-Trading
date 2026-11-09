"""Fail-closed IBKR official MCP allowlist and inventory/raw-read tests.

The vendored ``official_mcp_tools.json`` is a credential-free live
``tools/list`` snapshot. These tests never call api.ibkr.com.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config.schema import MCPServerConfig
from src.tools.mcp import MCPServerAdapter
from src.trading.connectors.ibkr.classification import IBKR_TOOL_CLASS
from src.trading.connectors.ibkr.oauth import IBKROAuthScopeError, assert_ibkr_readonly_remote_tool
from src.trading.connectors.ibkr.official_mcp_catalog import (
    ACCOUNT_INVENTORY_READS,
    EXCLUDED_READONLY_REMOTE_TOOLS,
    annotated_readonly_remote_tools,
    load_official_mcp_tools,
    mutating_remote_tools,
    readonly_remote_tools,
    validate_readonly_arguments,
)
from src.trading.service import (
    describe_ibkr_official_mcp_catalog,
    read_ibkr_official_mcp_inventory,
    read_ibkr_official_mcp_tool,
)

pytestmark = pytest.mark.unit

_MCP_URL = "https://api.ibkr.com/v1/api/mcp-public"


def test_vendored_inventory_classifies_all_official_tools() -> None:
    tools = load_official_mcp_tools()
    names = [str(tool["name"]) for tool in tools]
    assert len(tools) == 56
    assert len(set(names)) == 56
    annotated = annotated_readonly_remote_tools()
    mutating = mutating_remote_tools()
    allowed = readonly_remote_tools()
    assert len(annotated) == 39
    assert len(mutating) == 17
    assert annotated.isdisjoint(mutating)
    assert set(names) == annotated | mutating
    assert allowed == annotated - EXCLUDED_READONLY_REMOTE_TOOLS
    assert EXCLUDED_READONLY_REMOTE_TOOLS <= annotated
    assert "preview_order" not in allowed
    assert "preview_screener" not in allowed
    assert "get_combo_identifier" not in allowed
    for name in (
        "get_account_balances",
        "get_account_trades",
        "get_account_orders",
        "get_flow_type",
        "get_pa_allocation",
        "get_pa_performance_all_periods",
        "get_watchlists",
        "get_alerts",
        "list_saved_screeners",
        "get_order_instructions",
        "search_contracts",
        "get_price_snapshot",
        "get_price_history",
    ):
        assert name in allowed


def test_mutating_and_excluded_tools_are_curated_writes() -> None:
    for name in mutating_remote_tools() | EXCLUDED_READONLY_REMOTE_TOOLS:
        assert IBKR_TOOL_CLASS[name].value == "write"
    for name in readonly_remote_tools():
        assert name not in IBKR_TOOL_CLASS


def test_allowlisted_calls_pass_schema_defaults() -> None:
    validate_readonly_arguments("get_account_balances", {})
    validate_readonly_arguments("get_account_trades", {"period": "DAYS_7"})
    validate_readonly_arguments("get_account_orders", {"show_completed": True})
    validate_readonly_arguments("get_pa_allocation", {"type": "ALL"})
    for name, arguments in ACCOUNT_INVENTORY_READS:
        validate_readonly_arguments(name, arguments)


def test_schema_rejects_unknown_and_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="unknown argument"):
        validate_readonly_arguments("get_account_trades", {"period": "DAYS_7", "accountID": "U123"})
    with pytest.raises(ValueError, match="one of"):
        validate_readonly_arguments("get_account_trades", {"period": "week"})
    with pytest.raises(ValueError, match="missing required"):
        validate_readonly_arguments("get_pa_allocation", {})


def test_assert_readonly_allows_inventory_and_denies_mutations() -> None:
    assert_ibkr_readonly_remote_tool("get_account_balances")
    assert_ibkr_readonly_remote_tool("get_account_trades")
    with pytest.raises(IBKROAuthScopeError, match="submit_order"):
        assert_ibkr_readonly_remote_tool("submit_order")
    with pytest.raises(IBKROAuthScopeError, match="preview_order"):
        assert_ibkr_readonly_remote_tool("preview_order")
    with pytest.raises(IBKROAuthScopeError, match="not_a_real_tool"):
        assert_ibkr_readonly_remote_tool("not_a_real_tool")
    with pytest.raises(IBKROAuthScopeError, match="create_order_instruction"):
        assert_ibkr_readonly_remote_tool("create_order_instruction")


def _adapter(tmp_path, *, scopes: list[str]):
    cfg = MCPServerConfig.model_validate(
        {
            "type": "streamableHttp",
            "url": _MCP_URL,
            "auth": {"type": "oauth", "scopes": scopes, "cacheDir": str(tmp_path)},
            "enabledTools": ["*"],
        }
    )

    class _ForbiddenClient:
        async def __aenter__(self):
            raise AssertionError("blocked IBKR tools must not reach the MCP client")

        async def __aexit__(self, *args):
            return None

    return MCPServerAdapter("ibkr", cfg, client_factory=_ForbiddenClient)


def test_adapter_allows_safe_reads_and_denies_mutations_even_with_write_scopes(tmp_path) -> None:
    readonly = _adapter(tmp_path, scopes=["mcp.read"])
    trading = _adapter(
        tmp_path, scopes=["mcp.read", "mcp.write", "mcp.orders.submit"]
    )
    allowed = readonly.call_tool("get_account_balances", {})
    assert allowed["status"] == "error"
    assert allowed["error_type"] == "AssertionError"
    for name in ("submit_order", "preview_order"):
        payload = trading.call_tool(name, {})
        assert payload["status"] == "error"
        assert payload["error_type"] == "IBKROrderGateError"
        assert name in payload["error"]
    for name in ("create_order_instruction", "unknown_tool"):
        payload = trading.call_tool(name, {})
        assert payload["status"] == "error"
        assert payload["error_type"] == "IBKROAuthScopeError"
        assert name in payload["error"]


def test_cli_inventory_list_only_does_not_need_a_profile() -> None:
    from cli._legacy import EXIT_SUCCESS, cmd_connector_inventory

    assert cmd_connector_inventory(list_only=True) == EXIT_SUCCESS


def test_catalog_description_lists_allowlist_without_ibkr_calls() -> None:
    payload = describe_ibkr_official_mcp_catalog()
    assert payload["status"] == "ok"
    assert "get_account_balances" in payload["readonly_tools"]
    assert "submit_order" in payload["mutating_tools"]
    assert "preview_order" in payload["excluded_readonly_tools"]
    names = [row["tool"] for row in payload["account_inventory"]]
    assert names[0] == "get_flow_type"
    assert {"period": "DAYS_7"} in [row["arguments"] for row in payload["account_inventory"]]


def test_service_raw_read_and_inventory_use_schema_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    server = SimpleNamespace(
        url=_MCP_URL,
        enabled_tools=["*"],
        auth=SimpleNamespace(cache_dir="/tmp/vibe-token"),
    )

    class _Adapter:
        def __init__(self, server_name, server_config):  # noqa: ANN001
            assert server_name == "ibkr"
            assert server_config is server

        def call_tool(self, remote_name, arguments):  # noqa: ANN001
            calls.append((remote_name, dict(arguments)))
            return {"status": "ok", "structured_content": {remote_name: True}}

    monkeypatch.setattr(
        "src.config.loader.load_agent_config",
        lambda: SimpleNamespace(mcp_servers={"ibkr": server}),
    )
    monkeypatch.setattr("src.live.registry.has_cached_oauth_token", lambda *_: True)
    monkeypatch.setattr("src.tools.mcp.MCPServerAdapter", _Adapter)

    denied = read_ibkr_official_mcp_tool(
        "submit_order", "ibkr-live-official-mcp-readonly", {}
    )
    assert denied["status"] == "error"
    assert denied["error_type"] == "IBKROAuthScopeError"
    assert calls == []

    preview = read_ibkr_official_mcp_tool(
        "preview_order", "ibkr-live-official-mcp-readonly", {}
    )
    assert preview["status"] == "error"
    assert preview["error_type"] == "IBKROAuthScopeError"

    unknown = read_ibkr_official_mcp_tool(
        "not_a_real_tool", "ibkr-live-official-mcp-readonly", {}
    )
    assert unknown["status"] == "error"

    raw = read_ibkr_official_mcp_tool(
        "get_account_trades",
        "ibkr-live-official-mcp-readonly",
        {"period": "DAYS_7"},
    )
    assert raw["status"] == "ok"
    assert calls == [("get_account_trades", {"period": "DAYS_7"})]

    calls.clear()
    inventory = read_ibkr_official_mcp_inventory("ibkr-live-official-mcp-readonly")
    assert inventory["status"] == "ok"
    assert inventory["failed"] == []
    assert [name for name, _ in calls] == [name for name, _ in ACCOUNT_INVENTORY_READS]
    assert ("get_account_orders", {"show_completed": True}) in calls
    assert ("get_pa_allocation", {"type": "ALL"}) in calls
