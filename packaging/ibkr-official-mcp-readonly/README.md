# IBKR official MCP (read-only) — install bundle

Files in this directory:

| File | Purpose |
| --- | --- |
| `INSTALL.md` | Exact no-clone wheel install, seed, authorize, Grok stdio wiring |
| `agent.ibkr-readonly.json` | Canonical `~/.vibe-trading/agent.json` seed (`mcp.read` only) |
| `agent.ibkr-trading.json` | Explicit trading-capability seed (`mcp.read` + `mcp.write` + `mcp.orders.submit`) |
| `grok-bot-local-stdio.example.json` | Example local stdio MCP client config for `vibe-trading-mcp` |

IBKR's official connector is HTTP+OAuth. There is no IBKR stdio MCP binary.
`vibe-trading-mcp` is Vibe-Trading's own server, used after a local OAuth grant.
