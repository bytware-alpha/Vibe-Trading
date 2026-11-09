"""Offline gates for the default-off IBKR official MCP order path."""

from __future__ import annotations

import json

import pytest

from src.config.schema import MCPServerConfig
from src.tools.mcp import MCPServerAdapter
from src.trading.connectors.ibkr.oauth import IBKROAuthScopeError
from src.trading.connectors.ibkr.official_mcp_catalog import readonly_remote_tools
from src.trading.connectors.ibkr.order_gate import (
    append_audit,
    assert_ibkr_remote_tool,
    audit_path,
    confirm_hash,
    evaluate_margin,
    local_client_order_id,
    read_cached_oauth_scopes,
)
from src.trading.connectors.ibkr.orders import run_cancel, run_preview, run_submit

pytestmark = pytest.mark.unit

_SCOPES = ["mcp.read", "mcp.write", "mcp.orders.submit"]
_WIRE = {
    "contract_id_ex": "111@SMART",
    "side": "SELL",
    "order_type": "LIMIT",
    "tif": "DAY",
    "size": 1.0,
    "limit_price": 2.5,
    "is_position_close": True,
}


@pytest.fixture()
def order_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_ORDER_STATE_DIR", str(tmp_path))


def _book(**overrides: object) -> dict:
    calls: list[tuple[str, dict]] = []

    def call(name: str, arguments: dict) -> dict:
        calls.append((name, dict(arguments)))
        if name == "get_flow_type":
            return {"status": "ok", "structured_content": {"flow_type": overrides.get("flow", "AUTONOMOUS")}}
        if name == "get_account_summary":
            return {
                "status": "ok",
                "structured_content": overrides.get(
                    "summary",
                    {"excess_liquidity": 1000, "available_funds": 800},
                ),
            }
        if name == "get_account_positions":
            return {
                "status": "ok",
                "structured_content": {
                    "positions": overrides.get(
                        "positions",
                        [{"contract_id": 111, "position": 1, "asset_class": "OPT"}],
                    )
                },
            }
        if name == "preview_order":
            return {
                "status": "ok",
                "structured_content": overrides.get(
                    "preview",
                    {"amount": 250, "commission": 1, "total": 251, "errors": []},
                ),
            }
        if name == "submit_order":
            return {
                "status": "ok",
                "structured_content": {"order_id": "9", "order_status": "Submitted", "pending_question": None},
            }
        raise AssertionError(name)

    call.calls = calls  # type: ignore[attr-defined]
    return call  # type: ignore[return-value]


def test_order_tools_stay_off_the_read_allowlist() -> None:
    allowed = readonly_remote_tools()
    for name in ("preview_order", "submit_order", "get_order_status", "cancel_order", "modify_order"):
        assert name not in allowed


def test_flag_off_refuses_without_calling(order_state: None) -> None:
    call = _book()
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=False,
        granted_scopes=_SCOPES,
    )
    assert result["status"] == "error"
    assert "orderTrading" in result["error"]
    assert call.calls == []  # type: ignore[attr-defined]


def test_missing_orders_submit_scope_names_the_grant(order_state: None) -> None:
    call = _book()
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=True,
        granted_scopes=["mcp.read", "mcp.write"],
    )
    assert result["status"] == "error"
    assert "mcp.orders.submit" in result["error"]
    assert "mcp.read" in result["error"] and "mcp.write" in result["error"]
    assert call.calls == []  # type: ignore[attr-defined]


def test_review_and_approve_does_not_preview(order_state: None) -> None:
    call = _book(flow="REVIEW_AND_APPROVE")
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    assert result["status"] == "error"
    assert "REVIEW_AND_APPROVE" in result["error"]
    assert "preview_order" not in [name for name, _ in call.calls]  # type: ignore[attr-defined]


def test_opening_order_without_sma_is_refused(order_state: None) -> None:
    call = _book()
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="BUY",
        qty=1,
        limit_price=2.5,
        is_position_close=False,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    assert result["status"] == "refused"
    assert result["confirm_hash"] is None
    assert result["submitted"] is False
    assert "SMA" in result["error"]
    assert "submit_order" not in [name for name, _ in call.calls]  # type: ignore[attr-defined]


def test_close_without_sma_can_be_confirmed_when_funds_exist(order_state: None) -> None:
    call = _book()
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        asset="OPT",
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    assert result["status"] == "preview_only"
    assert result["submitted"] is False
    assert result["margin"]["sma_unverified"] is True
    assert result["margin"]["sma_impact"] == 250
    assert result["confirm_hash"]
    assert result["local_client_order_id"] == local_client_order_id(result["confirm_hash"])
    text = audit_path().read_text(encoding="utf-8")
    assert "access_token" not in text
    assert "Bearer " not in text


def test_market_order_requires_override(order_state: None) -> None:
    call = _book()
    result = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=None,
        order_type="MARKET",
        is_position_close=True,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    assert result["status"] == "error"
    assert "Limit orders only" in result["error"]
    assert call.calls == []  # type: ignore[attr-defined]


def test_submit_is_single_use_and_requires_a_fresh_match(order_state: None) -> None:
    call = _book()
    preview = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    token = preview["confirm_hash"]
    wrong = run_submit(call, "0" * 64, order_trading=True, granted_scopes=_SCOPES)
    assert wrong["status"] == "error"
    first = run_submit(call, token, order_trading=True, granted_scopes=_SCOPES)
    assert first["status"] == "submitted"
    assert first["local_client_order_id"] == preview["local_client_order_id"]
    submits = [args for name, args in call.calls if name == "submit_order"]  # type: ignore[attr-defined]
    assert len(submits) == 1
    assert submits[0]["_ibkr_confirm"] == token
    second = run_submit(call, token, order_trading=True, granted_scopes=_SCOPES)
    assert second["status"] == "error"
    assert "already" in second["error"].lower() or "cannot" in second["error"].lower()
    assert len([name for name, _ in call.calls if name == "submit_order"]) == 1  # type: ignore[attr-defined]


def test_stale_preview_is_not_submitted(order_state: None) -> None:
    from src.trading.connectors.ibkr.order_gate import load_confirm, save_confirm

    call = _book()
    preview = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    record = load_confirm(preview["confirm_hash"])
    assert record is not None
    record["created_at"] = "2020-01-01T00:00:00+00:00"
    save_confirm(record)
    result = run_submit(call, preview["confirm_hash"], order_trading=True, granted_scopes=_SCOPES)
    assert result["status"] == "error"
    assert "fresh preview" in result["error"]
    assert "submit_order" not in [name for name, _ in call.calls]  # type: ignore[attr-defined]


def test_changed_preview_does_not_reuse_the_hash(order_state: None) -> None:
    call = _book()
    preview = run_preview(
        call,
        contract_id_ex="111@SMART",
        side="SELL",
        qty=1,
        limit_price=2.5,
        is_position_close=True,
        order_trading=True,
        granted_scopes=_SCOPES,
    )
    call.preview = {"amount": 999, "commission": 1, "total": 1000, "errors": []}  # type: ignore[attr-defined]

    def call2(name: str, arguments: dict) -> dict:
        if name == "preview_order":
            return {"status": "ok", "structured_content": {"amount": 999, "commission": 1, "total": 1000, "errors": []}}
        return call(name, arguments)

    result = run_submit(call2, preview["confirm_hash"], order_trading=True, granted_scopes=_SCOPES)
    assert result["status"] == "error"
    assert "does not match" in result["error"]


def test_sma_math_and_preview_field() -> None:
    opening = evaluate_margin(
        summary={"excess_liquidity": 5000, "available_funds": 4000, "sma": 1000},
        balances={},
        positions={"positions": []},
        preview={"amount": 200},
        wire={"contract_id_ex": "1@SMART", "side": "BUY", "size": 2, "limit_price": 100, "is_position_close": False, "order_type": "LIMIT", "tif": "DAY"},
        asset="STK",
    )
    assert opening["allowed"] is True
    assert opening["sma_impact"] == -100
    assert opening["post_trade_sma"] == 900
    option_close = evaluate_margin(
        summary={"excess_liquidity": 10, "available_funds": 10},
        balances={},
        positions={"positions": [{"contract_id": 111, "position": 1}]},
        preview={"initial_margin": {"current": "1", "change": "0", "after": "1"}, "amount": 250},
        wire=_WIRE,
        asset="OPT",
    )
    assert option_close["allowed"] is True
    assert option_close["sma_unverified"] is True
    assert option_close["post_trade_sma"] is None
    assert "unverified" in option_close["note"]


def test_modify_and_instructions_stay_blocked() -> None:
    with pytest.raises(IBKROAuthScopeError, match="modify_order"):
        assert_ibkr_remote_tool(
            "modify_order", order_trading=True, granted_scopes=_SCOPES
        )
    with pytest.raises(IBKROAuthScopeError, match="create_order_instruction"):
        assert_ibkr_remote_tool(
            "create_order_instruction", order_trading=True, granted_scopes=_SCOPES
        )


def test_adapter_refuses_submit_without_confirm_even_when_armed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_ORDER_STATE_DIR", str(tmp_path))
    cache = tmp_path / "oauth"
    collection = cache / "mcp-oauth-token"
    collection.mkdir(parents=True)
    (collection / "token.json").write_text(
        json.dumps({"access_token": "super-secret-token", "scope": "mcp.read mcp.write mcp.orders.submit"}),
        encoding="utf-8",
    )
    assert read_cached_oauth_scopes(str(cache)) == {"mcp.read", "mcp.write", "mcp.orders.submit"}
    cfg = MCPServerConfig.model_validate(
        {
            "type": "streamableHttp",
            "url": "https://api.ibkr.com/v1/api/mcp-public",
            "orderTrading": True,
            "auth": {"type": "oauth", "scopes": _SCOPES, "cacheDir": str(cache)},
            "enabledTools": ["*"],
        }
    )

    class _Forbidden:
        async def __aenter__(self):
            raise AssertionError("submit must not reach the client without a confirm hash")

        async def __aexit__(self, *args):
            return None

    adapter = MCPServerAdapter("ibkr", cfg, client_factory=_Forbidden)
    payload = adapter.call_tool("submit_order", dict(_WIRE))
    assert payload["status"] == "error"
    assert payload["error_type"] == "IBKROrderGateError"
    assert "super-secret-token" not in payload["error"]
    blocked = adapter.call_tool("modify_order", {"order_id": "1"})
    assert blocked["error_type"] == "IBKROAuthScopeError"
    append_audit({"action": "note", "access_token": "super-secret-token", "ok": True})
    text = audit_path().read_text(encoding="utf-8")
    assert "super-secret-token" not in text


def test_cancel_confirm_is_single_use(order_state: None) -> None:
    from src.trading.connectors.ibkr.orders import run_cancel_preview

    preview = run_cancel_preview("55", order_trading=True, granted_scopes=_SCOPES)
    token = preview["confirm_hash"]
    calls: list[tuple[str, dict]] = []

    def call(name: str, arguments: dict) -> dict:
        calls.append((name, dict(arguments)))
        return {"status": "ok", "structured_content": {"order_id": "55", "order_status": "Cancelled"}}

    first = run_cancel(call, "55", token, order_trading=True, granted_scopes=_SCOPES)
    assert first["status"] == "cancel_sent"
    second = run_cancel(call, "55", token, order_trading=True, granted_scopes=_SCOPES)
    assert second["status"] == "error"
    assert len(calls) == 1
    assert confirm_hash("cancel_order", {"order_id": "55"}, {}) == token


def test_hash_changes_when_the_order_changes() -> None:
    left = confirm_hash("submit_order", _WIRE, {"amount": 250})
    changed = dict(_WIRE)
    changed["limit_price"] = 3
    right = confirm_hash("submit_order", changed, {"amount": 250})
    assert left != right
    assert local_client_order_id(left) != local_client_order_id(right)
