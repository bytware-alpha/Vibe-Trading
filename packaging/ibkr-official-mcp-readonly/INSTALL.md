# IBKR official MCP — wheel install (no git clone)

This is the **honest public-client** build of Vibe-Trading from fork branch
`cursor/ibkr-honest-oauth-a8c6`. It consumes Interactive Brokers' remote MCP
over HTTPS+OAuth. It does **not** impersonate a browser, does not retry HTTP
403 block pages, and does not collect brokerage credentials.

Account login happens only in **your** browser on **your** machine.

## What this package is (and is not)

| Surface | What it is |
| --- | --- |
| IBKR official MCP | Remote HTTP MCP at `https://api.ibkr.com/v1/api/mcp-public`. OAuth public client + PKCE + loopback callback. **No stdio IBKR binary exists. Do not invent one.** |
| `vibe-trading` CLI | Local app that writes `~/.vibe-trading/agent.json`, opens a browser for OAuth, caches tokens, and can read account/positions. |
| `vibe-trading-mcp` | Vibe-Trading's **own** MCP server (stdio by default). Research tools plus `trading_*` reads. After a successful local authorize, `trading_account` / `trading_positions` call IBKR through that cached grant. Live IBKR tools are **not** re-exported as raw `mcp_ibkr_*` wrappers. |

Grok Bot pointing its **remote HTTP connector** at `api.ibkr.com` is talking to IBKR, not to this package. A 403 HTML block page on that URL is an access block; this client will not retry it or switch to a Mozilla User-Agent.

**Supported Grok Bot path:** if Grok can spawn a **local stdio** MCP command on the same Mac that holds the OAuth cache, use `vibe-trading-mcp`. If Grok only accepts remote HTTPS MCP URLs, it cannot use this runtime unless you yourself run `vibe-trading-mcp --transport http` on loopback. That still is not IBKR's public endpoint.

## Same-machine constraint (required)

OAuth **must** run on the machine that:

1. Opens the system browser for IBKR login/consent.
2. Binds `http://localhost:8765/callback` (loopback only; IBKR redirects here).
3. Writes tokens to `~/.vibe-trading/live/ibkr/oauth` (mode 0700).

A cloud agent, a remote Grok connector, or another host **cannot** complete this grant. After tokens exist, `vibe-trading-mcp` spawned by Grok/Cursor on **that same user account** reuses the cache and will not open a browser (`interactive_oauth` is off when stdin is not a TTY).

Keep port **8765** free during `authorize`. Reuse it on reconnects so the registered redirect URI still matches.

## Install the wheel (no repository checkout)

Python 3.11+. Do not `git clone`.

From the artifact directory that contains the `.whl`, `constraints.txt`, and this file:

```bash
python3 -m venv "$HOME/.venvs/vibe-trading"
source "$HOME/.venvs/vibe-trading/bin/activate"
python -m pip install --upgrade pip

# 1) lockfile dependencies (hashed). 2) this wheel, no PyPI override of the app.
python -m pip install --require-hashes -r constraints.txt
python -m pip install --no-deps --no-index \
  "./vibe_trading_ai-0.1.19.post4-py3-none-any.whl"
```

If a previous honest wheel (`0.1.19` or `0.1.19.post1`) is already in this
venv, upgrade in place (no need to reinstall the hashed lock):

```bash
python -m pip install --force-reinstall --no-deps --no-index \
  "./vibe_trading_ai-0.1.19.post4-py3-none-any.whl"
```

Confirm the patched client, not PyPI 0.1.19:

```bash
python -c "from src.trading.connectors.ibkr.oauth import honest_user_agent; print(honest_user_agent())"
# expect: Vibe-Trading/0.1.19.post4
python -c "from src.tools import mcp as m; assert 'Mozilla/5.0' not in open(m.__file__).read()"
command -v vibe-trading
command -v vibe-trading-mcp
```

The `ibkr` extra (`ib_async`) is **not** required for official MCP. That extra is local TWS/Gateway only.

If `weasyprint` fails to install, that is a system library issue for PDF reports, not IBKR OAuth. For IBKR-only use, the hashed lock still expects it because it is a core package dependency.

## Read-only official connector seed

```bash
mkdir -p "$HOME/.vibe-trading"
cp agent.ibkr-readonly.json "$HOME/.vibe-trading/agent.json"
# or, after the wheel is installed:
python -c "from src.config.schema import format_ibkr_mcp_server_seed_json; print(format_ibkr_mcp_server_seed_json())" \
  > "$HOME/.vibe-trading/agent.json"
```

The seed requests **`mcp.read` only**. It does not include `mcp.write`,
`mcp.orders.submit`, or a client secret. IBKR metadata that advertises those
write/order scopes is ignored; the authorize URL is checked before a browser
opens, and local MCP write/order tool names stay refused. Annotated
account and market-data reads are allowlisted fail-closed from the official
`tools/list`; `preview_order`, `preview_screener`, and
`get_combo_identifier` stay blocked.

Do **not** run `vibe-trading connector configure` for this profile. That command writes local TWS/Gateway host/port only. Official MCP is configured exclusively via `~/.vibe-trading/agent.json`.

## Trading-capability seed (explicit opt-in)

Use this only when you want the IBKR connection grant to include the
documented trading scopes. It does **not** place orders. Local
`vibe-trading-mcp` still has no `trading_place_order` / `trading_cancel_order`,
and the IBKR adapter still refuses write/order tool names.

This is a separate seed from read-only. It is **not** produced by widening
`mcp.read` from server metadata.

```bash
cp agent.ibkr-trading.json "$HOME/.vibe-trading/agent.json"
# or:
python -c "from src.config.schema import format_ibkr_mcp_trading_server_seed_json; print(format_ibkr_mcp_trading_server_seed_json())" \
  > "$HOME/.vibe-trading/agent.json"

# Kill any leftover authorize listener from the earlier mcp.read attempt.
# Trading uses a separate token cache so that registration is not reused.
vibe-trading connector use ibkr-live-official-mcp-trading
vibe-trading connector authorize ibkr-live-official-mcp-trading
```

Authorize URL must show
`scope=mcp.read+mcp.write+mcp.orders.submit` (no extras). Tokens land in
`~/.vibe-trading/live/ibkr/oauth-trading`. Same-machine browser +
`http://localhost:8765/callback`.

## OAuth callback lifecycle (you run this on your Mac)

```bash
source "$HOME/.venvs/vibe-trading/bin/activate"
vibe-trading connector use ibkr-live-official-mcp-readonly
vibe-trading connector authorize ibkr-live-official-mcp-readonly
```

`authorize` must run in a real terminal (stdin is a TTY) so FastMCP may open a
browser. A Grok/Cursor-spawned `vibe-trading-mcp` process is non-interactive
and will only reuse tokens already cached on this account. If `agent.json`
is missing the IBKR seed, authorize prints the canonical JSON and exits.

Expected local sequence:

1. CLI starts a loopback callback server on `http://localhost:8765/callback`.
2. Browser opens IBKR authorize (`https://api.ibkr.com/oauth2/authorize`).
3. You sign in and consent in IBKR's page (this package never sees the password).
4. IBKR redirects to `http://localhost:8765/callback?...`.
5. CLI stores tokens under `~/.vibe-trading/live/ibkr/oauth` and prints how many read-only tools were discovered.

If the unauthenticated MCP URL returns HTML **403** with no `WWW-Authenticate`, stop. Do not retry with a Mozilla User-Agent.

After a successful grant:

```bash
vibe-trading connector check ibkr-live-official-mcp-readonly
vibe-trading connector account ibkr-live-official-mcp-readonly
vibe-trading connector positions ibkr-live-official-mcp-readonly
vibe-trading connector inventory --list
vibe-trading connector inventory ibkr-live-official-mcp-readonly
vibe-trading connector inventory ibkr-live-official-mcp-readonly \
  --tool get_account_trades --args '{"period":"DAYS_7"}'
```

## Live orders (default off)

`orderTrading` stays false in the seed. Live `preview_order` / `submit_order` /
`cancel_order` / `get_order_status` / `answer_order_question` run only when that
flag is true and the cached token includes `mcp.orders.submit`. The Mac steps,
including a preview that does not submit, are in `UPGRADE-TRADING.txt`.

## Expose local tools to Grok Bot / other MCP clients

Use Vibe-Trading's stdio server, **not** IBKR's URL:

```json
{
  "mcpServers": {
    "vibe-trading": {
      "command": "/Users/REPLACE_ME/.venvs/vibe-trading/bin/vibe-trading-mcp",
      "args": []
    }
  }
}
```

Put the venv `bin` on `command`, or set `command` to `vibe-trading-mcp` with `PATH` in `env`. `HOME` must be the same account that ran `authorize`.

Then, in the MCP client:

1. `trading_select_connection` with `ibkr-live-official-mcp-readonly`
2. `trading_check` / `trading_account` / `trading_positions` / `trading_inventory`
3. `trading_read` with an allowlisted tool name for any remaining official read

Order tools stay off this MCP server. Do not add `mcp.write`.

Optional loopback HTTP (still local, still not IBKR):

```bash
vibe-trading-mcp --transport http --host 127.0.0.1 --port 8900
```

Client URL: `http://127.0.0.1:8900/mcp`.
