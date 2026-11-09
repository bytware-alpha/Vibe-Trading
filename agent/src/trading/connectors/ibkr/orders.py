"""IBKR official MCP order flow. Default off. No network I/O in the pure steps.

``preview`` calls the broker only through the injected ``call`` function.
``submit`` calls ``preview_order`` again and refuses the order when that
preview no longer matches the confirm hash. ``submit_order`` is not given a
client order id because the official schema has no such field; the id is
local and is what the duplicate-hash check uses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping

from src.trading.connectors.ibkr.official_mcp_catalog import validate_readonly_arguments
from src.trading.connectors.ibkr.order_gate import (
    CONFIRM_ARGUMENT,
    PREVIEW_MAX_AGE_SECONDS,
    IBKROrderGateError,
    age_seconds,
    append_audit,
    assert_ibkr_remote_tool,
    audit_has_submit,
    confirm_hash,
    evaluate_margin,
    load_confirm,
    local_client_order_id,
    normalize_order,
    save_confirm,
    utc_now,
)

CallTool = Callable[[str, dict[str, Any]], dict[str, Any]]


def broker_bindings(profile_id: str | None) -> tuple[CallTool, bool, frozenset[str]]:
    """Build a live IBKR caller from the selected connector profile.

    Does not open a browser. A missing token cache is an error from the caller
    before this returns a usable trading call.
    """
    from src.config.loader import load_agent_config
    from src.live.registry import has_cached_oauth_token
    from src.tools.mcp import MCPServerAdapter
    from src.trading.connectors.ibkr.order_gate import read_cached_oauth_scopes
    from src.trading.profiles import profile_by_id

    profile = profile_by_id(profile_id)
    if profile.connector != "ibkr" or profile.transport != "remote_mcp":
        raise IBKROrderGateError(f"profile '{profile.id}' is not the IBKR official MCP connector.")
    server_name = str(profile.config.get("server") or "ibkr")
    server = (load_agent_config().mcp_servers or {}).get(server_name)
    if server is None or server.auth is None:
        raise IBKROrderGateError(f"MCP server '{server_name}' has no OAuth config.")
    if not has_cached_oauth_token(server.url, server.auth.cache_dir):
        raise IBKROrderGateError(
            "No cached IBKR token. Authorize on this Mac after Autonomous Trading "
            "is enabled. This command will not open a browser."
        )
    adapter = MCPServerAdapter(server_name, server, interactive_oauth=False)

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return adapter.call_tool(name, arguments)

    scopes = read_cached_oauth_scopes(server.auth.cache_dir)
    return call, bool(server.order_trading), scopes


def _content(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    data = result.get("data")
    if isinstance(data, dict):
        return data
    return dict(result)


def _refuse(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, "error_type": "IBKROrderGateError", **extra}


def _call_ok(call: CallTool, tool: str, arguments: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    validate_readonly_arguments(tool, arguments)
    result = call(tool, dict(arguments))
    if result.get("status") == "error":
        return None, {
            "status": "error",
            "error": result.get("error") or f"{tool} failed",
            "error_type": result.get("error_type") or "IBKROrderGateError",
            "tool": tool,
        }
    return _content(result), None


def run_preview(
    call: CallTool,
    *,
    contract_id_ex: str,
    side: str,
    qty: float,
    limit_price: float | None,
    tif: str = "DAY",
    order_type: str = "LIMIT",
    is_position_close: bool = False,
    asset: str = "OPT",
    allow_non_limit: bool = False,
    order_trading: bool = False,
    granted_scopes: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Preview one order and, when the margin gate allows it, issue a confirm hash.

    The hash is not a submit. ``submit_order`` is not called.
    """
    now = now or utc_now()
    try:
        assert_ibkr_remote_tool(
            "preview_order", order_trading=order_trading, granted_scopes=granted_scopes
        )
        prepared = normalize_order(
            contract_id_ex=contract_id_ex,
            side=side,
            qty=qty,
            limit_price=limit_price,
            tif=tif,
            order_type=order_type,
            is_position_close=is_position_close,
            asset=asset,
            allow_non_limit=allow_non_limit,
        )
    except IBKROrderGateError as exc:
        append_audit({"action": "preview_refused", "error": str(exc)})
        return _refuse(str(exc))

    wire = prepared["wire"]
    asset_token = prepared["asset"]
    flow, error = _call_ok(call, "get_flow_type", {})
    if error:
        append_audit({"action": "preview_refused", "error": error["error"], "wire": wire})
        return error
    flow_type = str((flow or {}).get("flow_type") or "")
    if flow_type != "AUTONOMOUS":
        message = (
            f"get_flow_type returned {flow_type or '(missing)'}. "
            "submit_order and preview_order require AUTONOMOUS. "
            "REVIEW_AND_APPROVE cannot place this order. "
            "An existing position in the parent account cannot be transferred "
            "into the AI Partition, so this path cannot close it until IBKR "
            "enables Autonomous Trading and the order is for a position that "
            "the autonomous session can see."
        )
        append_audit({"action": "preview_refused", "error": message, "wire": wire, "flow_type": flow_type})
        return _refuse(message, flow_type=flow_type, order=wire)

    summary, error = _call_ok(call, "get_account_summary", {})
    if error:
        return error
    positions, error = _call_ok(call, "get_account_positions", {})
    if error:
        return error
    preview, error = _call_ok(call, "preview_order", wire)
    if error:
        append_audit({"action": "preview_refused", "error": error["error"], "wire": wire})
        return error
    assert preview is not None
    margin = evaluate_margin(
        summary=summary,
        balances=None,
        positions=positions,
        preview=preview,
        wire=wire,
        asset=asset_token,
    )
    token = confirm_hash("submit_order", wire, preview)
    record = {
        "hash": token,
        "tool": "submit_order",
        "status": "pending" if margin["allowed"] else "refused",
        "wire": wire,
        "asset": asset_token,
        "preview": preview,
        "margin": margin,
        "flow_type": flow_type,
        "local_client_order_id": local_client_order_id(token),
        "created_at": now.isoformat(),
    }
    if margin["allowed"]:
        save_confirm(record)
    append_audit(
        {
            "action": "preview",
            "confirm_hash": token if margin["allowed"] else None,
            "local_client_order_id": record["local_client_order_id"] if margin["allowed"] else None,
            "wire": wire,
            "margin": margin,
            "flow_type": flow_type,
        }
    )
    return {
        "status": "preview_only" if margin["allowed"] else "refused",
        "submitted": False,
        "order": wire,
        "preview": preview,
        "margin": margin,
        "flow_type": flow_type,
        "confirm_hash": token if margin["allowed"] else None,
        "local_client_order_id": record["local_client_order_id"] if margin["allowed"] else None,
        "error": None if margin["allowed"] else "; ".join(margin["problems"]),
        "submit_command": (
            f"vibe-trading connector order-submit --confirm {token}" if margin["allowed"] else None
        ),
    }


def run_submit(
    call: CallTool,
    confirm: str,
    *,
    order_trading: bool = False,
    granted_scopes: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Submit the order named by ``confirm`` after a fresh matching preview."""
    now = now or utc_now()
    token = str(confirm or "").strip().lower()
    try:
        assert_ibkr_remote_tool(
            "submit_order", order_trading=order_trading, granted_scopes=granted_scopes
        )
    except IBKROrderGateError as exc:
        append_audit({"action": "submit_refused", "error": str(exc), "confirm_hash": token})
        return _refuse(str(exc))
    record = load_confirm(token)
    if record is None or record.get("tool") != "submit_order":
        return _refuse("Confirm hash was not found. Run order-preview again. Hashes are not reusable.")
    if record.get("status") != "pending":
        return _refuse(f"Confirm hash is {record.get('status')} and cannot be submitted again.")
    if audit_has_submit(token):
        return _refuse("This confirm hash was already submitted. Refusing a duplicate.")
    try:
        age = age_seconds(str(record.get("created_at") or ""), now)
    except ValueError:
        return _refuse("Confirm record has no timestamp. Preview again.")
    if age > PREVIEW_MAX_AGE_SECONDS or age < 0:
        return _refuse(
            f"Preview is {int(age)}s old. A submit requires a fresh preview "
            f"(<= {PREVIEW_MAX_AGE_SECONDS}s). Run order-preview again."
        )
    wire = record.get("wire")
    if not isinstance(wire, dict):
        return _refuse("Confirm record has no order. Preview again.")
    preview, error = _call_ok(call, "preview_order", wire)
    if error:
        return error
    assert preview is not None
    fresh = confirm_hash("submit_order", wire, preview)
    if fresh != token:
        append_audit({"action": "submit_refused", "confirm_hash": token, "error": "preview changed"})
        return _refuse(
            "The fresh preview does not match this confirm hash. "
            "Do not submit. Run order-preview again and confirm the new hash."
        )
    summary, error = _call_ok(call, "get_account_summary", {})
    if error:
        return error
    positions, error = _call_ok(call, "get_account_positions", {})
    if error:
        return error
    margin = evaluate_margin(
        summary=summary,
        balances=None,
        positions=positions,
        preview=preview,
        wire=wire,
        asset=str(record.get("asset") or "OPT"),
    )
    if not margin["allowed"]:
        append_audit({"action": "submit_refused", "confirm_hash": token, "margin": margin, "wire": wire})
        return _refuse("; ".join(margin["problems"]), margin=margin, order=wire, submitted=False)
    record["status"] = "submitting"
    record["margin"] = margin
    save_confirm(record)
    append_audit(
        {
            "action": "submit_attempt",
            "confirm_hash": token,
            "local_client_order_id": record.get("local_client_order_id"),
            "wire": wire,
        }
    )
    result = call("submit_order", {**wire, CONFIRM_ARGUMENT: token})
    record["status"] = "used"
    save_confirm(record)
    append_audit(
        {
            "action": "submit_result",
            "confirm_hash": token,
            "local_client_order_id": record.get("local_client_order_id"),
            "wire": wire,
            "margin": margin,
            "result_status": result.get("status"),
            "error_type": result.get("error_type"),
            "order_id": _content(result).get("order_id") if result.get("status") != "error" else None,
            "pending_question": _content(result).get("pending_question") if result.get("status") != "error" else None,
        }
    )
    if result.get("status") == "error":
        return {
            "status": "error",
            "submitted": False,
            "error": result.get("error"),
            "error_type": result.get("error_type"),
            "order": wire,
            "confirm_hash": token,
            "local_client_order_id": record.get("local_client_order_id"),
            "margin": margin,
        }
    body = _content(result)
    if body.get("pending_question"):
        _store_answer_challenge(body, token)
    return {
        "status": "submitted",
        "submitted": True,
        "order": wire,
        "result": body,
        "margin": margin,
        "confirm_hash": token,
        "local_client_order_id": record.get("local_client_order_id"),
        "sma_unverified": margin.get("sma_unverified"),
    }


def run_status(
    call: CallTool,
    order_id: str,
    *,
    order_trading: bool = False,
    granted_scopes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    try:
        assert_ibkr_remote_tool(
            "get_order_status", order_trading=order_trading, granted_scopes=granted_scopes
        )
    except IBKROrderGateError as exc:
        return _refuse(str(exc))
    body, error = _call_ok(call, "get_order_status", {"order_id": str(order_id)})
    if error:
        return error
    append_audit({"action": "order_status", "order_id": str(order_id)})
    return {"status": "ok", "order": body}


def run_cancel_preview(order_id: str, *, order_trading: bool, granted_scopes: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Issue a one-time cancel confirm. Does not call the broker."""
    try:
        assert_ibkr_remote_tool("cancel_order", order_trading=order_trading, granted_scopes=granted_scopes)
    except IBKROrderGateError as exc:
        return _refuse(str(exc))
    wire = {"order_id": str(order_id).strip()}
    if not wire["order_id"]:
        return _refuse("order_id is required.")
    token = confirm_hash("cancel_order", wire, {})
    save_confirm(
        {
            "hash": token,
            "tool": "cancel_order",
            "status": "pending",
            "wire": wire,
            "preview": {},
            "created_at": utc_now().isoformat(),
            "local_client_order_id": local_client_order_id(token),
        }
    )
    append_audit({"action": "cancel_preview", "confirm_hash": token, "wire": wire})
    return {
        "status": "cancel_preview",
        "submitted": False,
        "order_id": wire["order_id"],
        "confirm_hash": token,
        "submit_command": f"vibe-trading connector order-cancel --order-id {wire['order_id']} --confirm {token}",
    }


def run_cancel(
    call: CallTool,
    order_id: str,
    confirm: str,
    *,
    order_trading: bool = False,
    granted_scopes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    try:
        assert_ibkr_remote_tool("cancel_order", order_trading=order_trading, granted_scopes=granted_scopes)
    except IBKROrderGateError as exc:
        return _refuse(str(exc))
    token = str(confirm or "").strip().lower()
    record = load_confirm(token)
    wire = {"order_id": str(order_id).strip()}
    if record is None or record.get("tool") != "cancel_order" or record.get("wire") != wire:
        return _refuse("Cancel confirm hash does not match this order_id.")
    if record.get("status") != "pending" or audit_has_submit(token):
        return _refuse("This cancel confirm was already used.")
    record["status"] = "submitting"
    save_confirm(record)
    append_audit({"action": "cancel_attempt", "confirm_hash": token, "wire": wire})
    result = call("cancel_order", {**wire, CONFIRM_ARGUMENT: token})
    record["status"] = "used"
    save_confirm(record)
    append_audit({"action": "cancel_result", "confirm_hash": token, "wire": wire, "result_status": result.get("status")})
    if result.get("status") == "error":
        return {"status": "error", "submitted": False, "error": result.get("error"), "error_type": result.get("error_type")}
    return {"status": "cancel_sent", "submitted": True, "result": _content(result), "confirm_hash": token}


def _store_answer_challenge(body: Mapping[str, Any], parent: str) -> None:
    question = body.get("pending_question")
    if not isinstance(question, dict):
        return
    answers = question.get("answers")
    if not isinstance(answers, list):
        answers = []
    wire = {
        "question_id": question.get("question_id") or question.get("id"),
        "correlation_id": body.get("correlation_id"),
    }
    save_confirm(
        {
            "hash": f"pending-{parent[:16]}",
            "tool": "answer_challenge",
            "status": "pending",
            "wire": wire,
            "answers": answers,
            "created_at": utc_now().isoformat(),
        }
    )


def run_answer(
    call: CallTool,
    *,
    question_id: str,
    value: str,
    correlation_id: str,
    confirm: str,
    order_trading: bool = False,
    granted_scopes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Forward one broker question. ``value`` must be one of the broker's answers."""
    try:
        assert_ibkr_remote_tool(
            "answer_order_question", order_trading=order_trading, granted_scopes=granted_scopes
        )
    except IBKROrderGateError as exc:
        return _refuse(str(exc))
    token = str(confirm or "").strip().lower()
    record = load_confirm(token)
    wire = {
        "question_id": str(question_id).strip(),
        "value": str(value),
        "correlation_id": str(correlation_id).strip(),
    }
    expected = confirm_hash("answer_order_question", wire, {})
    if token != expected or record is None or record.get("wire") != wire or record.get("status") != "pending":
        return _refuse(
            "Answer confirm hash does not match this question. "
            "Run order-answer without --confirm to print the hash, then pass that hash once."
        )
    if audit_has_submit(token):
        return _refuse("This answer confirm was already used.")
    record["status"] = "submitting"
    save_confirm(record)
    append_audit({"action": "answer_attempt", "confirm_hash": token, "question_id": wire["question_id"]})
    result = call("answer_order_question", {**wire, CONFIRM_ARGUMENT: token})
    record["status"] = "used"
    save_confirm(record)
    append_audit({"action": "answer_result", "confirm_hash": token, "wire": wire, "result_status": result.get("status")})
    if result.get("status") == "error":
        return {"status": "error", "submitted": False, "error": result.get("error"), "error_type": result.get("error_type")}
    return {"status": "answer_sent", "submitted": True, "result": _content(result)}


def prepare_answer_confirm(
    *,
    question_id: str,
    value: str,
    correlation_id: str,
    order_trading: bool,
    granted_scopes: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    try:
        assert_ibkr_remote_tool(
            "answer_order_question", order_trading=order_trading, granted_scopes=granted_scopes
        )
    except IBKROrderGateError as exc:
        return _refuse(str(exc))
    wire = {
        "question_id": str(question_id).strip(),
        "value": str(value),
        "correlation_id": str(correlation_id).strip(),
    }
    token = confirm_hash("answer_order_question", wire, {})
    save_confirm(
        {
            "hash": token,
            "tool": "answer_order_question",
            "status": "pending",
            "wire": wire,
            "preview": {},
            "created_at": utc_now().isoformat(),
            "local_client_order_id": local_client_order_id(token),
        }
    )
    append_audit({"action": "answer_preview", "confirm_hash": token, "wire": {**wire, "value": "[held]"}})
    return {
        "status": "answer_preview",
        "submitted": False,
        "confirm_hash": token,
        "question_id": wire["question_id"],
        "note": "value is not printed back into the audit. Pass --confirm with this hash to send it once.",
    }
