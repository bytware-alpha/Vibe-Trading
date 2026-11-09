"""Local IB Gateway limit-order tool.

This package places no orders unless a person previews a single limit order,
receives a one-time confirm hash, and submits that hash while trading is
enabled in the local config. It talks only to a loopback TWS / IB Gateway
socket. It does not use the official IBKR MCP.
"""

__version__ = "0.1.0"
