"""Bridge auth, routes, and the install contract."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fakes import ACCOUNT, fig_order

from ibkr_gw_trade.audit import AuditLog
from ibkr_gw_trade.bridge import App, make_httpd
from ibkr_gw_trade.bridge import main as bridge_main
from ibkr_gw_trade.cli import main
from ibkr_gw_trade.config import init_config, load_settings
from ibkr_gw_trade.errors import TradeError
from ibkr_gw_trade.token import ensure_token

ROOT = Path(__file__).resolve().parents[1]


def test_init_writes_the_kill_switch_once(tmp_path):
    path, created = init_config(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert created is True
    assert raw == {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 4001,
        "account": "U23708642",
        "read_client_id": 17,
        "trade_client_id": 18,
        "sma_buffer": 1000,
        "timeout": 15,
        "bridge_host": "0.0.0.0",
        "bridge_port": 4472,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    raw["enabled"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    again, created_again = init_config(tmp_path)
    assert created_again is False
    assert json.loads(again.read_text(encoding="utf-8"))["enabled"] is True


def test_bad_config_fails_closed(tmp_path):
    missing = load_settings(tmp_path)
    assert missing.enabled is False
    assert missing.account == ""
    init_config(tmp_path)
    path = tmp_path / "config.json"

    def rewrite(**changes):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update(changes)
        path.write_text(json.dumps(raw), encoding="utf-8")

    rewrite(enabled="true")
    with pytest.raises(TradeError) as text_flag:
        load_settings(tmp_path)
    assert text_flag.value.code == "bad_config"
    rewrite(enabled=False, host="10.0.0.8")
    with pytest.raises(TradeError) as host:
        load_settings(tmp_path)
    assert host.value.code == "bad_config"
    rewrite(host="127.0.0.1", read_client_id=17, trade_client_id=17)
    with pytest.raises(TradeError) as clients:
        load_settings(tmp_path)
    assert clients.value.code == "bad_config"


def test_token_mode_and_symlink(tmp_path):
    old = os.umask(0o022)
    try:
        token = ensure_token(tmp_path / "bridge.token")
    finally:
        os.umask(old)
    assert len(token) >= 24
    assert (tmp_path / "bridge.token").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    os.chmod(tmp_path / "bridge.token", 0o644)
    with pytest.raises(TradeError) as loose:
        ensure_token(tmp_path / "bridge.token")
    assert loose.value.code == "token_mode"

    target = tmp_path / "other"
    target.write_text("keep\n", encoding="utf-8")
    audit_link = tmp_path / "audit.jsonl"
    audit_link.symlink_to(target)
    with pytest.raises(TradeError):
        AuditLog(audit_link).write({"event": "preview", "decision": "ok"}, 1.0)
    assert target.read_text(encoding="utf-8") == "keep\n"
    token_link = tmp_path / "linked.token"
    token_link.symlink_to(target)
    with pytest.raises(TradeError) as linked:
        ensure_token(token_link)
    assert linked.value.code == "bad_path"


def test_cli_fig_preview_does_not_place(runtime, capsys):
    assert main(["init"]) == 0
    rc = main(
        [
            "preview",
            "--symbol",
            "FIG",
            "--sec-type",
            "STK",
            "--side",
            "SELL",
            "--qty",
            "1",
            "--limit",
            "9999",
            "--tif",
            "DAY",
        ],
        broker=runtime.broker,
        now=runtime.clock,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PREVIEW ONLY" in out
    assert "confirm withheld" in out
    assert "LIMIT 9999" in out
    assert "conId=4242" in out
    assert runtime.script.placed == []
    assert main(["submit"]) == 2
    status = main(["status"], broker=runtime.broker, now=runtime.clock)
    assert status == 0
    assert "none" in capsys.readouterr().out


def test_bridge_startup_does_not_print_the_token(runtime, capsys):
    seen = {}

    def fake_serve(app):
        seen["host"] = app.settings().bridge_host
        seen["port"] = app.settings().bridge_port
        seen["token"] = app.bearer()
        return 0

    rc = bridge_main(serve=fake_serve)
    out, err = capsys.readouterr()
    assert rc == 0
    assert seen["host"] == "0.0.0.0"
    assert seen["port"] == 4472
    assert seen["token"] not in out
    assert seen["token"] not in err
    assert "0600" in out
    assert "clientId 17" in out
    assert "clientId 18" in out


def test_loose_token_refuses_bridge_start(runtime, capsys):
    path = runtime.home / "bridge.token"
    path.write_text("a" * 32 + "\n", encoding="utf-8")
    os.chmod(path, 0o644)

    def fake_serve(app):
        raise AssertionError(app)

    rc = bridge_main(serve=fake_serve)
    assert rc == 2
    assert "0600" in capsys.readouterr().err


def test_http_gates(runtime):
    token = ensure_token(runtime.home / "bridge.token")
    app = App(home=runtime.home, broker=runtime.broker, now=runtime.clock)
    httpd = make_httpd(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        status, body = _request(port, "GET", "/summary")
        assert status == 200
        tags = [row["tag"] for row in body["summary"]]
        assert "AccountType" not in tags
        for name in ("SMA", "EquityWithLoanValue", "RegTEquity", "RegTMargin", "Leverage", "Cushion"):
            assert name in tags
        assert all(ib.client_id == 17 and ib.readonly is True for ib in runtime.script.ibs)

        status_body = _request(port, "GET", "/status")[1]
        assert status_body["connected"] is True
        assert status_body["readClientId"] == 17
        assert status_body["tradeClientId"] == 18
        assert status_body["tradingEnabled"] is False
        assert status_body["account"] == ACCOUNT

        denied, denied_body = _request(port, "POST", "/preview", fig_order(), token=None)
        assert denied == 401
        assert denied_body["error"] == "unauthorized"
        assert runtime.script.placed == []

        sneaky = "sneaky-token-value-should-not-appear"
        sneaky_status, _sneaky_body = _request(port, "POST", "/preview", fig_order(), token=sneaky)
        assert sneaky_status == 401

        preview_status, preview = _request(port, "POST", "/preview", fig_order(), token=token)
        assert preview_status == 200
        assert preview["decision"] == "withheld"
        assert preview["confirm"] is None
        assert preview["snapshot"]["limit"] == "9999"
        assert runtime.script.placed == []

        runtime.set_enabled(True)
        ok_status, armed = _request(port, "POST", "/preview", fig_order(), token=token)
        assert ok_status == 200
        assert armed["confirm"]
        extra, _extra_body = _request(
            port,
            "POST",
            "/submit",
            {"confirm": armed["confirm"], "qty": "9"},
            token=token,
        )
        assert extra == 400
        assert runtime.script.placed == []
        placed_status, placed = _request(port, "POST", "/submit", {"confirm": armed["confirm"]}, token=token)
        assert placed_status == 200
        assert placed["orderId"] == "42"
        assert len(runtime.script.placed) == 1
        assert runtime.script.placed[0][0].client_id == 18
        assert runtime.script.placed[0][0].readonly is False
        again, again_body = _request(port, "POST", "/submit", {"confirm": armed["confirm"]}, token=token)
        assert again == 409
        assert again_body["error"] == "hash_used"
        assert len(runtime.script.placed) == 1

        market, market_body = _request(port, "POST", "/preview", fig_order(orderType="MKT"), token=token)
        assert market == 400
        assert market_body["error"] == "not_limit"
        batch, batch_body = _request(port, "POST", "/preview", [], token=token)
        assert batch == 400
        assert batch_body["error"] == "batch_refused"
        assert _request(port, "GET", "/preview")[0] == 405
        assert _request(port, "POST", "/summary", {}, token=token)[0] == 405
        assert _request(port, "GET", "/missing")[0] == 404

        audit = (runtime.home / "audit.jsonl").read_text(encoding="utf-8")
        assert token not in audit
        assert sneaky not in audit
        assert armed["confirm"] not in audit
        assert armed["confirm"][:8] in audit
    finally:
        httpd.shutdown()
        thread.join(5)
        httpd.server_close()


def test_install_doc_has_the_fig_preview_and_no_submit_command():
    text = (ROOT / "MAC-INSTALL.txt").read_text(encoding="utf-8")
    command = "ibkr-gw-trade preview --symbol FIG --sec-type STK --side SELL --qty 1 --limit 9999 --tif DAY"
    assert command in text
    assert "source ~/ibkr-bridge/venv/bin/activate" in text
    assert "127.0.0.1" in text
    assert "4001" in text
    assert "0.0.0.0:4472" in text
    assert "clientId 17" in text or "read_client_id 17" in text
    assert "clientId 18" in text or "trade_client_id 18" in text
    assert "0600" in text
    assert "AMD" not in text
    for line in text.splitlines():
        assert not line.strip().startswith("ibkr-gw-trade submit")
    assert re.search(r"^WHEEL_SHA256=([0-9a-f]{64}|pending)$", text, re.MULTILINE)


def _request(port: int, method: str, path: str, body=None, token=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, json.loads(payload)
