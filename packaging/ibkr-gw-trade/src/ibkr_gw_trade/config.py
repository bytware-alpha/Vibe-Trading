"""Local kill switch and gateway endpoints.

Trading stays off unless ``config.json`` contains ``"enabled": true``.
The account, host, and client ids are config, not hardcoded gates.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ibkr_gw_trade.errors import TradeError
from ibkr_gw_trade.money import parse_plain_number

DEFAULT_ACCOUNT = "U23708642"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001
DEFAULT_READ_CLIENT_ID = 17
DEFAULT_TRADE_CLIENT_ID = 18
DEFAULT_SMA_BUFFER = Decimal(1000)
DEFAULT_TIMEOUT = 15.0
DEFAULT_BRIDGE_HOST = "0.0.0.0"
DEFAULT_BRIDGE_PORT = 4472
CONFIRM_TTL_SECONDS = 120
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BRIDGE_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost", "::1"})
CONFIG_KEYS = frozenset(
    {
        "enabled",
        "host",
        "port",
        "account",
        "read_client_id",
        "trade_client_id",
        "sma_buffer",
        "timeout",
        "bridge_host",
        "bridge_port",
    }
)
_ACCOUNT = re.compile(r"^[A-Za-z0-9]{5,20}$")


def state_dir() -> Path:
    override = os.environ.get("IBKR_GW_TRADE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ibkr-gw-trade"


def assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise TradeError("bad_path", f"{path.name} must not be a symlink")


def secure_dir(path: Path) -> None:
    assert_not_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise TradeError("bad_path", f"{path.name} must not be a symlink")
    os.chmod(path, 0o700)


@dataclass(frozen=True)
class Settings:
    enabled: bool
    host: str
    port: int
    account: str
    read_client_id: int
    trade_client_id: int
    sma_buffer: Decimal
    timeout: float
    bridge_host: str
    bridge_port: int
    home: Path

    def require_account(self) -> None:
        if not self.account:
            raise TradeError(
                "account_not_configured",
                "account is not set; run ibkr-gw-trade init",
            )


def default_config_dict(account: str = DEFAULT_ACCOUNT) -> dict:
    return {
        "enabled": False,
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "account": account,
        "read_client_id": DEFAULT_READ_CLIENT_ID,
        "trade_client_id": DEFAULT_TRADE_CLIENT_ID,
        "sma_buffer": int(DEFAULT_SMA_BUFFER),
        "timeout": int(DEFAULT_TIMEOUT),
        "bridge_host": DEFAULT_BRIDGE_HOST,
        "bridge_port": DEFAULT_BRIDGE_PORT,
    }


def config_path(home: Path | None = None) -> Path:
    return (home or state_dir()) / "config.json"


def init_config(home: Path | None = None, account: str = DEFAULT_ACCOUNT) -> tuple[Path, bool]:
    """Write the default config if it is missing. Never overwrite an existing file."""
    root = home or state_dir()
    secure_dir(root)
    path = root / "config.json"
    assert_not_symlink(path)
    if path.exists():
        return path, False
    _validate_account(account)
    payload = json.dumps(default_config_dict(account), indent=2) + "\n"
    _write_private(path, payload.encode("utf-8"))
    return path, True


def load_settings(home: Path | None = None) -> Settings:
    root = home or state_dir()
    path = root / "config.json"
    if not path.exists():
        return _settings_from_mapping({}, root)
    assert_not_symlink(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TradeError("bad_config", "config.json is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise TradeError("bad_config", "config.json must be an object")
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise TradeError("bad_config", "unknown config keys: " + ", ".join(unknown))
    return _settings_from_mapping(raw, root)


def _settings_from_mapping(raw: dict, home: Path) -> Settings:
    enabled = _parse_bool(raw.get("enabled", False), "enabled")
    host = str(raw.get("host", DEFAULT_HOST)).strip()
    if host not in LOOPBACK_HOSTS:
        raise TradeError("bad_config", "host must be the loopback gateway (127.0.0.1)")
    port = _parse_port(raw.get("port", DEFAULT_PORT), "port")
    account = str(raw.get("account", "") or "").strip()
    if account:
        _validate_account(account)
    read_id = _parse_client_id(raw.get("read_client_id", DEFAULT_READ_CLIENT_ID), "read_client_id")
    trade_id = _parse_client_id(raw.get("trade_client_id", DEFAULT_TRADE_CLIENT_ID), "trade_client_id")
    if read_id == trade_id:
        raise TradeError("bad_config", "read_client_id and trade_client_id must differ")
    buffer = _parse_buffer(raw.get("sma_buffer", int(DEFAULT_SMA_BUFFER)))
    timeout = _parse_timeout(raw.get("timeout", int(DEFAULT_TIMEOUT)))
    bridge_host = str(raw.get("bridge_host", DEFAULT_BRIDGE_HOST)).strip()
    if bridge_host not in BRIDGE_HOSTS:
        raise TradeError("bad_config", "bridge_host must be 0.0.0.0 or loopback")
    bridge_port = _parse_port(raw.get("bridge_port", DEFAULT_BRIDGE_PORT), "bridge_port")
    return Settings(
        enabled=enabled,
        host=host,
        port=port,
        account=account,
        read_client_id=read_id,
        trade_client_id=trade_id,
        sma_buffer=buffer,
        timeout=timeout,
        bridge_host=bridge_host,
        bridge_port=bridge_port,
        home=home,
    )


def _validate_account(account: str) -> None:
    if not _ACCOUNT.fullmatch(account):
        raise TradeError("bad_config", "account must be 5-20 letters or digits")


def _parse_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TradeError("bad_config", f"{name} must be a JSON boolean")
    return value


def _parse_port(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TradeError("bad_config", f"{name} must be an integer")
    if not 1 <= value <= 65535:
        raise TradeError("bad_config", f"{name} is out of range")
    return value


def _parse_client_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TradeError("bad_config", f"{name} must be an integer")
    if not 1 <= value <= 999:
        raise TradeError("bad_config", f"{name} must be from 1 to 999")
    return value


def _parse_buffer(value: object) -> Decimal:
    try:
        text = parse_plain_number(value, "sma_buffer")
    except (TypeError, ValueError) as exc:
        raise TradeError("bad_config", "sma_buffer must be a non-negative USD amount") from exc
    number = Decimal(text)
    if number < 0:
        raise TradeError("bad_config", "sma_buffer must be a non-negative USD amount")
    return number.quantize(Decimal("0.01"))


def _parse_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TradeError("bad_config", "timeout must be a number of seconds")
    if not 1 <= float(value) <= 120:
        raise TradeError("bad_config", "timeout must be from 1 to 120 seconds")
    return float(value)


def _write_private(path: Path, payload: bytes) -> None:
    assert_not_symlink(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
