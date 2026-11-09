"""Default-off gate for IBKR official MCP live orders.

Nothing in this module opens a socket. The CLI calls it before any
``submit_order`` / ``cancel_order`` / ``answer_order_question`` request.
A confirm hash is single-use and is tied to the exact wire arguments plus
the preview payload. The audit log stores decisions only, never tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.trading.connectors.ibkr.oauth import IBKROAuthScopeError, parse_oauth_scope_tokens
from src.trading.connectors.ibkr.official_mcp_catalog import (
    IBKR_ORDER_MUTATING_TOOLS,
    IBKR_ORDER_TOOLS,
    readonly_remote_tools,
)

ORDERS_SUBMIT_SCOPE = "mcp.orders.submit"
PREVIEW_MAX_AGE_SECONDS = 120
OPTION_MULTIPLIER = 100
CONFIRM_ARGUMENT = "_ibkr_confirm"
_TOKEN_KEY = re.compile(r"(token|secret|authorization|password)", re.IGNORECASE)


class IBKROrderGateError(IBKROAuthScopeError):
    """A live IBKR order was refused before it was sent."""


def state_dir() -> Path:
    """Return the local directory for confirms and the order audit log."""
    override = os.environ.get("IBKR_ORDER_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("~/.vibe-trading/live/ibkr").expanduser()


def audit_path() -> Path:
    return state_dir() / "order-audit.jsonl"


def _confirm_path(token: str) -> Path:
    return state_dir() / "confirms" / f"{token}.json"


def canonical_json(value: Any) -> str:
    """Return a stable JSON encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def confirm_hash(tool: str, wire: Mapping[str, Any], preview: Mapping[str, Any] | None) -> str:
    """Hash the tool name, wire arguments, and preview fingerprint."""
    body = {
        "tool": tool,
        "wire": {key: wire[key] for key in sorted(wire)},
        "preview": _preview_fingerprint(preview or {}),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def local_client_order_id(token: str) -> str:
    """Stable local id for one confirmed order. IBKR's submit schema has no client id field."""
    return "vt" + token[:32]


def _preview_fingerprint(preview: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "amount",
        "commission",
        "total",
        "errors",
        "warnings",
        "funds",
        "initial_margin",
        "maintenance_margin",
        "position",
    )
    return {key: preview.get(key) for key in keys if key in preview}


def assert_ibkr_remote_tool(
    remote_name: str,
    *,
    order_trading: bool = False,
    granted_scopes: Sequence[str] | object = (),
) -> None:
    """Allow a read, or a live-order tool only when the local flag and scope match.

    Args:
        remote_name: Official IBKR MCP tool name.
        order_trading: ``mcpServers.ibkr.orderTrading``. Default false.
        granted_scopes: Scopes actually present on the cached token, not the
            scopes the seed requested.

    Raises:
        IBKROrderGateError: The tool is an order tool and the gate is closed.
        IBKROAuthScopeError: The tool is not a vetted read and not an order tool.
    """
    if remote_name in readonly_remote_tools():
        return
    scopes = set(parse_oauth_scope_tokens(granted_scopes))
    if remote_name in IBKR_ORDER_TOOLS:
        if not order_trading:
            raise IBKROrderGateError(
                f"IBKR order tool '{remote_name}' is off. "
                "Set mcpServers.ibkr.orderTrading to true only after the cached "
                "grant includes mcp.orders.submit. Default is off."
            )
        if ORDERS_SUBMIT_SCOPE not in scopes:
            granted = " ".join(sorted(scopes)) or "(none)"
            raise IBKROrderGateError(
                f"IBKR order tool '{remote_name}' refused: cached grant is missing "
                f"mcp.orders.submit (granted: {granted}). A REVIEW_AND_APPROVE "
                "connection receives mcp.read and mcp.write only. Re-authorize "
                "after Autonomous Trading is enabled on the IBKR account."
            )
        return
    raise IBKROAuthScopeError(
        "IBKR official MCP refuses remote tool "
        f"'{remote_name}'. Reads stay on the inventory allowlist; "
        "alerts, watchlists, screeners, instructions, feedback, and "
        "modify_order stay blocked."
    )


def require_confirm_for_call(remote_name: str, arguments: Mapping[str, Any], token: str | None) -> None:
    """Refuse a mutating order call whose confirm record is not mid-submit."""
    if remote_name not in IBKR_ORDER_MUTATING_TOOLS:
        return
    if not token:
        raise IBKROrderGateError(
            f"IBKR '{remote_name}' requires a one-time confirm hash from "
            "the matching preview/cancel/answer command. Refusing to send."
        )
    record = load_confirm(token)
    if record is None:
        raise IBKROrderGateError(
            f"IBKR confirm '{token[:12]}' was not found. Preview again; hashes are not reusable."
        )
    if record.get("status") != "submitting":
        raise IBKROrderGateError(
            f"IBKR confirm '{token[:12]}' is {record.get('status')}, not an armed submit. "
            "Refusing a duplicate or unconfirmed order."
        )
    if record.get("tool") != remote_name:
        raise IBKROrderGateError(
            f"IBKR confirm was issued for {record.get('tool')}, not {remote_name}."
        )
    if record.get("wire") != {key: arguments[key] for key in sorted(arguments)}:
        raise IBKROrderGateError(
            "IBKR confirm hash does not match these order arguments. Refusing to send."
        )


def normalize_order(
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
) -> dict[str, Any]:
    """Return the IBKR wire object, or raise before any network call."""
    contract = str(contract_id_ex or "").strip()
    if not contract:
        raise IBKROrderGateError("contract_id_ex is required. Resolve it with get_option_data or search_contracts.")
    side_token = str(side or "").strip().upper()
    if side_token not in {"BUY", "SELL"}:
        raise IBKROrderGateError("side must be BUY or SELL.")
    try:
        size = float(qty)
    except (TypeError, ValueError) as exc:
        raise IBKROrderGateError("qty must be a positive number.") from exc
    if size <= 0:
        raise IBKROrderGateError("qty must be positive.")
    style = str(order_type or "LIMIT").strip().upper()
    if style != "LIMIT" and not allow_non_limit:
        raise IBKROrderGateError(
            f"order_type {style} is refused. Limit orders only unless --allow-non-limit is set."
        )
    if style not in {"LIMIT", "MARKET", "STOP", "STOP_LIMIT"}:
        raise IBKROrderGateError(f"unsupported order_type {style}.")
    tif_token = str(tif or "DAY").strip().upper()
    if tif_token not in {"DAY", "OVT", "OND"}:
        raise IBKROrderGateError("tif must be DAY, OVT, or OND.")
    asset_token = str(asset or "OPT").strip().upper()
    if asset_token not in {"OPT", "STK"}:
        raise IBKROrderGateError("asset must be OPT or STK. Agentic Trading does not submit other types.")
    wire: dict[str, Any] = {
        "contract_id_ex": contract,
        "side": side_token,
        "order_type": style,
        "tif": tif_token,
        "size": size,
        "is_position_close": bool(is_position_close),
    }
    if style in {"LIMIT", "STOP_LIMIT"}:
        if limit_price is None:
            raise IBKROrderGateError("limit_price is required for a limit order. This client does not invent a price.")
        try:
            limit = float(limit_price)
        except (TypeError, ValueError) as exc:
            raise IBKROrderGateError("limit_price must be numeric.") from exc
        if limit <= 0:
            raise IBKROrderGateError("limit_price must be positive.")
        wire["limit_price"] = limit
    return {"wire": wire, "asset": asset_token}


def find_number(payload: Any, names: Sequence[str]) -> float | None:
    """Return the first numeric value stored under one of ``names``."""
    wanted = {name.lower() for name in names}
    found = _walk_number(payload, wanted)
    return found


def _walk_number(payload: Any, wanted: set[str]) -> float | None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in wanted:
                number = _as_float(value)
                if number is not None:
                    return number
            nested = _walk_number(value, wanted)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _walk_number(item, wanted)
            if nested is not None:
                return nested
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").replace("$", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(value, Mapping):
        for key in ("after", "current", "value", "amount"):
            if key in value:
                number = _as_float(value.get(key))
                if number is not None:
                    return number
    return None


def held_long_qty(positions_payload: Any, contract_id_ex: str) -> float | None:
    """Return the long quantity whose contract id matches ``contract_id_ex``."""
    prefix = str(contract_id_ex).split("@", 1)[0]
    rows = _position_rows(positions_payload)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cid = str(row.get("contract_id") or row.get("contract_id_ex") or "")
        cid_prefix = cid.split("@", 1)[0]
        if cid_prefix and cid_prefix == prefix:
            qty = _as_float(row.get("position"))
            if qty is None:
                qty = _as_float(row.get("qty"))
            return qty
    return None


def _position_rows(payload: Any) -> list[Any]:
    if isinstance(payload, Mapping):
        rows = payload.get("positions")
        if isinstance(rows, list):
            return rows
        structured = payload.get("structured_content")
        if isinstance(structured, Mapping):
            return _position_rows(structured)
    if isinstance(payload, list):
        return payload
    return []


def premium_impact(*, asset: str, qty: float, limit_price: float | None, preview: Mapping[str, Any]) -> float | None:
    """Dollar SMA impact magnitude. Options use 100% of premium; stock buys use 50%."""
    amount = find_number(preview, ("amount",))
    if amount is not None:
        magnitude = abs(amount)
    elif limit_price is None:
        return None
    else:
        multiplier = OPTION_MULTIPLIER if asset == "OPT" else 1
        magnitude = abs(qty * limit_price * multiplier)
    if asset == "STK":
        return magnitude * 0.5
    return magnitude


def evaluate_margin(
    *,
    summary: Mapping[str, Any] | None,
    balances: Mapping[str, Any] | None,
    positions: Any,
    preview: Mapping[str, Any] | None,
    wire: Mapping[str, Any],
    asset: str,
) -> dict[str, Any]:
    """Apply Tushar's SMA / ExcessLiquidity / AvailableFunds rule.

    Opening orders are refused when SMA is absent. A sell-to-close of a long
    position at or under the held quantity may proceed when ExcessLiquidity
    and AvailableFunds are present; the result says SMA was unverified.
    """
    sources = [summary or {}, balances or {}, preview or {}]
    sma = None
    for source in sources:
        sma = find_number(source, ("sma", "reg_t_sma", "regt_sma", "special_memorandum_account"))
        if sma is not None:
            break
    excess = find_number(summary or {}, ("excess_liquidity", "excessLiquidity"))
    if excess is None and preview:
        excess = find_number(preview, ("excess_liquidity",))
    available = find_number(summary or {}, ("available_funds", "availableFunds"))
    if available is None and preview:
        available = find_number(preview, ("available_funds",))
    held = held_long_qty(positions, str(wire.get("contract_id_ex") or ""))
    side = str(wire.get("side") or "")
    qty = float(wire.get("size") or 0)
    closing_long = (
        side == "SELL"
        and bool(wire.get("is_position_close"))
        and held is not None
        and held > 0
        and qty <= held + 1e-9
    )
    limit_price = wire.get("limit_price")
    limit = float(limit_price) if isinstance(limit_price, (int, float)) else None
    impact = premium_impact(asset=asset, qty=qty, limit_price=limit, preview=preview or {})
    post_sma = None if sma is None or impact is None else (sma - impact if not closing_long else sma + impact)
    preview_errors = (preview or {}).get("errors") if isinstance(preview, Mapping) else None
    problems: list[str] = []
    if isinstance(preview_errors, list) and preview_errors:
        problems.append("preview_order returned errors; submit is refused.")
    if excess is None or available is None:
        problems.append(
            "ExcessLiquidity and AvailableFunds must both be present before any order. "
            f"excess_liquidity={excess} available_funds={available}."
        )
    elif excess <= 0 or available <= 0:
        problems.append(
            f"ExcessLiquidity ({excess}) and AvailableFunds ({available}) must both be positive."
        )
    sma_unverified = sma is None
    if closing_long:
        if sma_unverified:
            note = "SMA was unverified. Closing sell of a long position is allowed only because ExcessLiquidity and AvailableFunds were present and qty <= held qty."
        else:
            note = "SMA was present. Closing sell adds the premium back."
    else:
        note = "Opening order."
        if side == "SELL":
            problems.append(
                "SELL is allowed only as a sell-to-close of a long position with qty <= held qty. "
                "Short selling is not enabled. Held quantity could not be matched to this contract."
                if held is None
                else f"SELL qty {qty} exceeds held long qty {held}, or the position is not long."
            )
        if sma_unverified:
            problems.append(
                "Reg T SMA was not in get_account_summary, get_account_balances, or preview_order. "
                "Opening orders are refused until SMA is present."
            )
        elif post_sma is not None and post_sma < 0:
            problems.append(f"Post-trade SMA would be {post_sma}, below zero.")
    return {
        "allowed": not problems,
        "problems": problems,
        "sma": sma,
        "sma_unverified": sma_unverified,
        "excess_liquidity": excess,
        "available_funds": available,
        "held_qty": held,
        "closing_long": closing_long,
        "sma_impact": (impact if closing_long else (None if impact is None else -impact)),
        "post_trade_sma": post_sma,
        "note": note,
        "preview_initial_margin": (preview or {}).get("initial_margin") if isinstance(preview, Mapping) else None,
        "preview_maintenance_margin": (preview or {}).get("maintenance_margin") if isinstance(preview, Mapping) else None,
    }


def save_confirm(record: Mapping[str, Any]) -> None:
    token = str(record["hash"])
    path = _confirm_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(redact(dict(record)), sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load_confirm(token: str) -> dict[str, Any] | None:
    path = _confirm_path(token)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def audit_has_submit(token: str) -> bool:
    path = audit_path()
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("confirm_hash") == token and row.get("action") in {"submit_attempt", "cancel_attempt", "answer_attempt"}:
            return True
    return False


def append_audit(event: Mapping[str, Any]) -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    row = redact(dict(event))
    row.setdefault("at", datetime.now(timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(row) + "\n")
    os.chmod(path, 0o600)


def redact(value: Any) -> Any:
    """Drop token-like keys and bearer strings from an audit payload."""
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _TOKEN_KEY.search(str(key)):
                continue
            cleaned[str(key)] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "[redacted]"
    return value


def read_cached_oauth_scopes(cache_dir: str | None) -> frozenset[str]:
    """Read scope tokens from the FastMCP token cache. Token values are not returned."""
    if not cache_dir:
        return frozenset()
    root = Path(cache_dir).expanduser() / "mcp-oauth-token"
    if not root.is_dir():
        return frozenset()
    found: set[str] = set()
    try:
        files = [path for path in root.rglob("*") if path.is_file()]
    except OSError:
        return frozenset()
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        found.update(_scopes_from_cache_json(payload))
    return frozenset(found)


def _scopes_from_cache_json(payload: Any, *, depth: int = 0) -> set[str]:
    if depth > 4 or not isinstance(payload, Mapping):
        return set()
    found: set[str] = set()
    for key in ("scope", "scopes"):
        if key in payload:
            found.update(parse_oauth_scope_tokens(payload.get(key)))
    for key in ("token", "tokens", "value", "data"):
        child = payload.get(key)
        if isinstance(child, Mapping):
            found.update(_scopes_from_cache_json(child, depth=depth + 1))
    return found


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def age_seconds(created_at: str, now: datetime) -> float:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()
