"""Bearer token for bridge POST routes. The file mode stays 0600."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from ibkr_gw_trade.config import assert_not_symlink, secure_dir
from ibkr_gw_trade.errors import TradeError

TOKEN_NAME = "bridge.token"
MIN_TOKEN_LENGTH = 24


def token_path(home: Path) -> Path:
    return home / TOKEN_NAME


def ensure_token(path: Path) -> str:
    secure_dir(path.parent)
    assert_not_symlink(path)
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise TradeError("token_mode", f"{path.name} mode is {mode:o}; expected 0600")
        if path.stat().st_size > 256:
            raise TradeError("token_invalid", "bearer token file is unusable")
        return _parse_token(path.read_bytes())
    token = secrets.token_urlsafe(32)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (token + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return token


def token_matches(presented: str, expected: str) -> bool:
    if not presented or len(presented) != len(expected):
        return False
    return secrets.compare_digest(presented, expected)


def _parse_token(raw: bytes) -> str:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TradeError("token_invalid", "bearer token file is unusable") from exc
    text = text.removesuffix("\n")
    if not text or "\n" in text or len(text) < MIN_TOKEN_LENGTH or any(ch.isspace() for ch in text):
        raise TradeError("token_invalid", "bearer token file is unusable")
    return text
