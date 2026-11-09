"""HTTP bridge. GET stays open. POST /preview and POST /submit need the bearer token."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ibkr_gw_trade.audit import AuditLog
from ibkr_gw_trade.broker import IbBroker
from ibkr_gw_trade.config import load_settings
from ibkr_gw_trade.confirms import ConfirmStore
from ibkr_gw_trade.errors import BrokerError, TradeError
from ibkr_gw_trade.gates import parse_order_spec
from ibkr_gw_trade.service import (
    gateway_status,
    preview_order,
    read_orders,
    read_positions,
    read_summary,
    read_trades,
    submit_order,
)
from ibkr_gw_trade.token import ensure_token, token_matches, token_path

MAX_BODY = 65536
_CLIENT_ERRORS = {
    "bad_json",
    "bad_request",
    "unexpected_fields",
    "batch_refused",
    "not_limit",
    "bad_tif",
    "bad_qty",
    "bad_price",
    "bad_side",
    "bad_sec_type",
    "bad_contract",
    "bad_multiplier",
}
_SERVER_ERRORS = {"bad_config", "token_mode", "token_invalid", "bad_path", "internal", "audit_blocked"}


class App:
    def __init__(self, *, home, broker: IbBroker, now=None) -> None:
        self.home = home
        self.broker = broker
        self.now = now
        self.store = ConfirmStore(home / "confirms.json")
        self.audit = AuditLog(home / "audit.jsonl")

    def settings(self):
        settings = load_settings(self.home)
        self.broker.settings = settings
        return settings

    def bearer(self) -> str:
        token = ensure_token(token_path(self.home))
        if token not in self.audit.forbidden:
            self.audit.forbid(token)
        return token

    def get(self, handler: Handler) -> None:
        path = route(handler.path)
        try:
            settings = self.settings()
            if path == "/status":
                handler.send_json(200, gateway_status(settings=settings, broker=self.broker))
            elif path == "/summary":
                handler.send_json(200, read_summary(settings=settings, broker=self.broker))
            elif path == "/positions":
                handler.send_json(200, read_positions(settings=settings, broker=self.broker))
            elif path == "/orders":
                handler.send_json(200, read_orders(settings=settings, broker=self.broker, audit=self.audit, now=self.now))
            elif path == "/trades":
                handler.send_json(200, read_trades(settings=settings, broker=self.broker, audit=self.audit, now=self.now))
            elif path in {"/preview", "/submit"}:
                handler.send_json(405, {"error": "method_not_allowed", "message": "method not allowed"})
            else:
                handler.send_json(404, {"error": "not_found", "message": "not found"})
        except TradeError as exc:
            handler.send_json(http_status(exc), public_error(exc))

    def post(self, handler: Handler) -> None:
        path = route(handler.path)
        if path not in {"/preview", "/submit"}:
            handler.send_json(405, {"error": "method_not_allowed", "message": "method not allowed"})
            return
        raw = read_body(handler)
        if raw is None:
            return
        try:
            token = self.bearer()
        except TradeError as exc:
            handler.send_json(http_status(exc), public_error(exc))
            return
        if not authorized(handler, token):
            self._audit_unauthorized(path)
            handler.send_json(401, {"error": "unauthorized", "message": "bearer token required"})
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            handler.send_json(400, {"error": "bad_json", "message": "body must be JSON"})
            return
        try:
            settings = self.settings()
            if path == "/preview":
                if not isinstance(body, dict):
                    raise TradeError("batch_refused", "one order object is required")
                spec = parse_order_spec(body)
                report = preview_order(
                    spec,
                    settings=settings,
                    broker=self.broker,
                    store=self.store,
                    audit=self.audit,
                    now=self.now,
                )
                handler.send_json(200, report)
                return
            if not isinstance(body, dict):
                raise TradeError("batch_refused", "one confirm object is required")
            if set(body) - {"confirm"}:
                raise TradeError("unexpected_fields", "submit accepts only confirm")
            if not isinstance(body.get("confirm"), str):
                raise TradeError("bad_request", "confirm is required")
            report = submit_order(
                body["confirm"],
                settings=settings,
                broker=self.broker,
                store=self.store,
                audit=self.audit,
                now=self.now,
            )
            handler.send_json(200, report)
        except TradeError as exc:
            handler.send_json(http_status(exc), public_error(exc))

    def _audit_unauthorized(self, path: str) -> None:
        try:
            settings = self.settings()
            account = settings.account
            enabled = settings.enabled
        except TradeError:
            account = ""
            enabled = False
        event = "submit" if path == "/submit" else "preview"
        self.audit.write(
            {
                "event": event,
                "decision": "refused",
                "reason": "unauthorized",
                "account": account,
                "tradingEnabled": enabled,
            },
            _now(self.now),
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.app.get(self)

    def do_POST(self) -> None:
        self.server.app.post(self)

    def do_PUT(self) -> None:
        self.send_json(405, {"error": "method_not_allowed", "message": "method not allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        try:
            request = str(args[0]) if args else ""
            parts = request.split()
            if len(parts) >= 2 and parts[0] in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}:
                path = parts[1].split("?", 1)[0]
                sys.stderr.write(f"ibkr-gw-trade {parts[0]} {path}\n")
        except (IndexError, TypeError, ValueError):
            return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_httpd(app: App, host: str, port: int) -> Server:
    httpd = Server((host, port), Handler)
    httpd.app = app
    return httpd


def serve_forever(app: App) -> int:
    settings = app.settings()
    try:
        httpd = make_httpd(app, settings.bridge_host, settings.bridge_port)
    except OSError as exc:
        print(
            f"error: cannot listen on {settings.bridge_host}:{settings.bridge_port}: {exc}",
            file=sys.stderr,
        )
        return 3
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0


def main(argv=None, *, serve=None, broker: IbBroker | None = None, now=None) -> int:
    del argv
    try:
        settings = load_settings()
        active = broker or IbBroker(settings)
        active.settings = settings
        app = App(home=settings.home, broker=active, now=now)
        path = token_path(settings.home)
        ensure_token(path)
        _announce(settings, path)
        runner = serve or serve_forever
        return int(runner(app) or 0)
    except BrokerError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 3
    except TradeError as exc:
        print(f"refused: {exc.code}: {exc}", file=sys.stderr)
        return 2


def _announce(settings, path) -> None:
    print(f"ibkr-gw-trade bridge listening on {settings.bridge_host}:{settings.bridge_port}")
    print(f"account {settings.account or '(not set)'}")
    print(f"read clientId {settings.read_client_id} readonly")
    print(f"trade clientId {settings.trade_client_id} used only for preview and submit")
    print(f"trading enabled: {str(settings.enabled).lower()}")
    print(f"bearer file: {path} (mode 0600)")
    print("GET /status /summary /positions /orders /trades are open")
    print("POST /preview and POST /submit require the bearer token")


def route(path: str) -> str:
    clean = urlsplit(path).path or "/"
    if len(clean) > 1:
        clean = clean.rstrip("/")
    return clean


def read_body(handler: Handler) -> bytes | None:
    length_header = handler.headers.get("Content-Length")
    if length_header is None or not str(length_header).isdigit():
        handler.send_json(400, {"error": "bad_request", "message": "Content-Length is required"})
        return None
    length = int(length_header)
    if length > MAX_BODY:
        handler.send_json(413, {"error": "bad_request", "message": "body is too large"})
        return None
    return handler.rfile.read(length)


def authorized(handler: Handler, token: str) -> bool:
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return token_matches(header[7:].strip(), token)


def http_status(exc: TradeError) -> int:
    if isinstance(exc, BrokerError):
        return 503
    if exc.code in _CLIENT_ERRORS:
        return 400
    if exc.code == "unauthorized":
        return 401
    if exc.code in _SERVER_ERRORS:
        return 500
    return 409


def public_error(exc: TradeError) -> dict:
    body = {"error": exc.code, "message": str(exc)}
    report = exc.details.get("report") if isinstance(exc.details, dict) else None
    if isinstance(report, dict):
        safe = dict(report)
        safe.pop("confirm", None)
        body["report"] = safe
    return body


def _now(clock) -> float:
    import time

    return clock() if clock is not None else time.time()
