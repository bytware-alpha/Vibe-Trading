"""Command line. There is no batch submit and no market-order flag."""

from __future__ import annotations

import argparse
import json
import sys

from ibkr_gw_trade import __version__
from ibkr_gw_trade.audit import AuditLog
from ibkr_gw_trade.broker import IbBroker
from ibkr_gw_trade.config import DEFAULT_ACCOUNT, init_config, load_settings
from ibkr_gw_trade.confirms import ConfirmStore
from ibkr_gw_trade.errors import BrokerError, TradeError
from ibkr_gw_trade.gates import parse_order_spec
from ibkr_gw_trade.service import (
    format_placement,
    format_report,
    format_status,
    preview_order,
    read_orders,
    read_trades,
    submit_order,
)


def main(argv: list[str] | None = None, *, broker: IbBroker | None = None, now=None, serve=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.command == "init":
            return _init(args.account)
        settings = load_settings()
        active = broker or IbBroker(settings)
        active.settings = settings
        audit = AuditLog(settings.home / "audit.jsonl")
        store = ConfirmStore(settings.home / "confirms.json")
        if args.command == "preview":
            return _preview(args, settings, active, store, audit, now)
        if args.command == "submit":
            return _submit(args, settings, active, store, audit, now)
        if args.command == "status":
            return _status(settings, active, audit, now)
        if args.command == "bridge":
            from ibkr_gw_trade.bridge import main as bridge_main

            return bridge_main(serve=serve, broker=active, now=now)
    except BrokerError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 3
    except TradeError as exc:
        report = exc.details.get("report") if isinstance(exc.details, dict) else None
        if isinstance(report, dict) and "snapshot" in report:
            sys.stdout.write(format_report(report))
        print(f"refused: {exc.code}: {exc}", file=sys.stderr)
        return 2
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-gw-trade",
        description=(
            "Local IB Gateway LIMIT orders. Every live order needs a fresh preview "
            "confirm hash. No market orders and no batch submit. Trading stays off "
            "until config enabled is true."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write ~/.ibkr-gw-trade/config.json if it is missing")
    init.add_argument("--account", default=DEFAULT_ACCOUNT)
    preview = sub.add_parser("preview", help="what-if one LIMIT order and maybe print a confirm hash")
    preview.add_argument("--symbol", default="")
    preview.add_argument("--sec-type", required=True)
    preview.add_argument("--side", required=True)
    preview.add_argument("--qty", required=True)
    preview.add_argument("--limit", required=True)
    preview.add_argument("--tif", required=True)
    preview.add_argument("--exchange", default="SMART")
    preview.add_argument("--currency", default="USD")
    preview.add_argument("--expiry", default="")
    preview.add_argument("--strike", default="")
    preview.add_argument("--right", default="")
    preview.add_argument("--con-id", default="")
    preview.add_argument("--multiplier", default="")
    preview.add_argument("--json", action="store_true")
    submit = sub.add_parser("submit", help="place the one LIMIT order for a confirm hash")
    submit.add_argument("--confirm", required=True)
    sub.add_parser("status", help="print open orders and today's fills")
    sub.add_parser("bridge", help="serve the local HTTP bridge")
    return parser


def _init(account: str) -> int:
    path, created = init_config(account=account)
    if created:
        print(f"wrote {path}")
        print("enabled: false")
    else:
        print(f"config already exists, left unchanged: {path}")
    return 0


def _preview(args, settings, broker, store, audit, now) -> int:
    spec = parse_order_spec(_preview_payload(args))
    report = preview_order(spec, settings=settings, broker=broker, store=store, audit=audit, now=now)
    if args.json:
        json.dump(report, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_report(report))
    return 0


def _submit(args, settings, broker, store, audit, now) -> int:
    report = submit_order(args.confirm, settings=settings, broker=broker, store=store, audit=audit, now=now)
    sys.stdout.write(format_placement(report))
    return 0


def _status(settings, broker, audit, now) -> int:
    orders = read_orders(settings=settings, broker=broker, audit=audit, now=now)
    trades = read_trades(settings=settings, broker=broker, audit=audit, now=now)
    sys.stdout.write(format_status(orders, trades))
    return 0


def _preview_payload(args) -> dict:
    payload = {
        "symbol": args.symbol,
        "secType": args.sec_type,
        "side": args.side,
        "qty": args.qty,
        "limit": args.limit,
        "tif": args.tif,
        "exchange": args.exchange,
        "currency": args.currency,
        "orderType": "LMT",
    }
    if args.expiry:
        payload["expiry"] = args.expiry
    if args.strike:
        payload["strike"] = args.strike
    if args.right:
        payload["right"] = args.right
    if args.con_id:
        payload["conId"] = args.con_id
    if args.multiplier:
        payload["multiplier"] = args.multiplier
    return payload
