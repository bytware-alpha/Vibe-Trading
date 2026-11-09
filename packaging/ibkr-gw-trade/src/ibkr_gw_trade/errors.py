"""Errors the CLI and bridge can show without a traceback."""


class TradeError(Exception):
    """A refusal or a local validation failure. No order was newly placed."""

    def __init__(self, code: str, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class BrokerError(TradeError):
    """The local gateway connection failed or rejected a call."""

    def __init__(self, message: str, *, code: str = "gateway_error", details: dict | None = None) -> None:
        super().__init__(code, message, details=details)
