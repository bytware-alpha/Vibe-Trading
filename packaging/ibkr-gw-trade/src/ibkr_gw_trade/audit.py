"""Append-only audit log. Confirm hashes and bearer tokens are not written."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ibkr_gw_trade.config import assert_not_symlink, secure_dir
from ibkr_gw_trade.errors import TradeError

AUDIT_KEYS = (
    "account",
    "avgPrice",
    "commission",
    "conId",
    "decision",
    "event",
    "excessLiquidity",
    "availableFunds",
    "execId",
    "filled",
    "fills",
    "hashPrefix",
    "initMarginAfter",
    "initMarginBefore",
    "limit",
    "localSymbol",
    "maintMarginAfter",
    "maintMarginBefore",
    "openOrders",
    "orderId",
    "positionQty",
    "price",
    "projectedAvailableFunds",
    "projectedExcessLiquidity",
    "projectedSma",
    "qty",
    "reason",
    "remaining",
    "secType",
    "side",
    "sma",
    "smaBuffer",
    "smaImpact",
    "smaImpactReason",
    "status",
    "symbol",
    "tif",
    "time",
    "tradingEnabled",
    "ts",
    "warning",
)
_SECRET_MIN = 16


def iso_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    def __init__(self, path: Path, forbidden: tuple[str, ...] = ()) -> None:
        self.path = path
        self.forbidden = list(forbidden)

    def forbid(self, secret: str) -> None:
        if secret and len(secret) >= _SECRET_MIN:
            self.forbidden.append(secret)

    def write(self, event: dict, now: float) -> None:
        payload = {key: event[key] for key in AUDIT_KEYS if key in event and event[key] is not None}
        payload["ts"] = iso_time(now)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        for secret in self.forbidden:
            if secret and len(secret) >= _SECRET_MIN and secret in raw:
                raise TradeError("audit_blocked", "refusing to write a secret to the audit log")
        if re.search(r"\b[0-9a-f]{64}\b", raw):
            raise TradeError("audit_blocked", "refusing to write a confirm hash to the audit log")
        secure_dir(self.path.parent)
        assert_not_symlink(self.path)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.write(fd, (raw + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)


def hash_prefix(confirm: str | None) -> str | None:
    if not confirm:
        return None
    return confirm[:8]
