"""Single-use confirm hashes. The hash is a local capability, not an order id."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from ibkr_gw_trade.config import CONFIRM_TTL_SECONDS, assert_not_symlink, secure_dir
from ibkr_gw_trade.errors import TradeError

_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEEP_SECONDS = 86400


def canonical(snapshot: dict) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sign(snapshot: dict, nonce: str) -> str:
    body = canonical(snapshot) + "\n" + nonce
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def diff_snapshot(left: dict, right: dict) -> list[str]:
    keys = sorted(set(left) | set(right))
    return [key for key in keys if left.get(key) != right.get(key)]


@dataclass(frozen=True)
class ConfirmRecord:
    digest: str
    nonce: str
    snapshot: dict
    expires: float
    used: bool


class ConfirmStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def issue(self, snapshot: dict, now: float) -> tuple[str, float]:
        nonce = secrets.token_hex(16)
        digest = sign(snapshot, nonce)
        expires = now + CONFIRM_TTL_SECONDS

        def mutate(data: dict) -> None:
            _prune(data, now)
            for record in data["confirms"].values():
                if not record.get("used") and not record.get("superseded"):
                    record["superseded"] = True
            data["confirms"][digest] = {
                "expires": expires,
                "used": False,
                "superseded": False,
                "nonce": nonce,
                "snapshot": snapshot,
            }

        self._update(mutate)
        return digest, expires

    def lookup(self, confirm: str, now: float) -> ConfirmRecord:
        digest = _normalize_hash(confirm)
        found: dict = {}

        def mutate(data: dict) -> None:
            _prune(data, now)
            record = data["confirms"].get(digest)
            if record is None:
                raise TradeError("hash_unknown", "unknown confirm")
            found["record"] = record
            if sign(record["snapshot"], record["nonce"]) != digest:
                raise TradeError("hash_unknown", "unknown confirm")

        self._update(mutate)
        record = found["record"]
        if record.get("superseded"):
            raise TradeError("hash_superseded", "a newer preview replaced this confirm")
        if now >= float(record["expires"]):
            raise TradeError("hash_expired", f"confirm expired after {CONFIRM_TTL_SECONDS} seconds")
        if record.get("used"):
            raise TradeError("hash_used", "confirm was already used")
        return ConfirmRecord(
            digest=digest,
            nonce=str(record["nonce"]),
            snapshot=dict(record["snapshot"]),
            expires=float(record["expires"]),
            used=False,
        )

    def consume(self, confirm: str, now: float) -> None:
        digest = _normalize_hash(confirm)

        def mutate(data: dict) -> None:
            record = data["confirms"].get(digest)
            if record is None:
                raise TradeError("hash_unknown", "unknown confirm")
            if record.get("superseded"):
                raise TradeError("hash_superseded", "a newer preview replaced this confirm")
            if now >= float(record["expires"]):
                raise TradeError("hash_expired", f"confirm expired after {CONFIRM_TTL_SECONDS} seconds")
            if record.get("used"):
                raise TradeError("hash_used", "confirm was already used")
            if sign(record["snapshot"], record["nonce"]) != digest:
                raise TradeError("hash_unknown", "unknown confirm")
            record["used"] = True

        self._update(mutate)

    def _update(self, mutator) -> None:
        secure_dir(self.path.parent)
        assert_not_symlink(self.path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.chmod(self.path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 10_000_000)
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {"confirms": {}}
            if not isinstance(data, dict) or not isinstance(data.get("confirms"), dict):
                data = {"confirms": {}}
            mutator(data)
            payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _normalize_hash(confirm: object) -> str:
    if not isinstance(confirm, str):
        raise TradeError("hash_unknown", "unknown confirm")
    digest = confirm.strip().lower()
    if not _HASH.fullmatch(digest):
        raise TradeError("hash_unknown", "unknown confirm")
    return digest


def _prune(data: dict, now: float) -> None:
    stale = [
        key
        for key, record in data["confirms"].items()
        if float(record.get("expires", 0)) < now - _KEEP_SECONDS
    ]
    for key in stale:
        del data["confirms"][key]
