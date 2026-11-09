"""Drop-in replacement for ~/ibkr-bridge/bridge.py.

Install ibkr-gw-trade into the same virtualenv, then run this file with that
interpreter. GET routes stay open. POST /preview and POST /submit use the
bearer file ~/.ibkr-gw-trade/bridge.token.
"""

from ibkr_gw_trade.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
