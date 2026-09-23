"""Login helper for the unofficial ``robin_stocks`` Robinhood API.

Robinhood has no official public API for equities. ``robin_stocks`` wraps
the private API used by the Robinhood app, so it can break without notice.
It is imported lazily so backtesting and paper trading on CSV data never
need it installed.
"""

from __future__ import annotations

from .config import Credentials


def login(creds: Credentials):
    """Log in and return the ``robin_stocks.robinhood`` module."""
    import robin_stocks.robinhood as rh

    mfa_code = None
    if creds.totp_secret:
        import pyotp

        mfa_code = pyotp.TOTP(creds.totp_secret).now()

    rh.login(
        username=creds.username,
        password=creds.password,
        mfa_code=mfa_code,
        store_session=True,
    )
    return rh
