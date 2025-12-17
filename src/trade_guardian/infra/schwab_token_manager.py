# trade_guardian/infra/schwab_token_manager.py
from __future__ import annotations

import os

def fetch_schwab_token() -> str:
    """
    Resolve Schwab OAuth access token.
    Priority:
      1) env SCHWAB_ACCESS_TOKEN
      2) legacy module shipped with this project: schwab_token_manager_legacy.py
      3) legacy global module name: schwab_token_manager (if user has it on PYTHONPATH)
    """
    tok = os.getenv("SCHWAB_ACCESS_TOKEN", "").strip()
    if tok:
        return tok

    # local legacy copy (recommended)
    try:
        from .schwab_token_manager_legacy import fetch_schwab_token as legacy_fetch
        tok = (legacy_fetch() or "").strip()
        if tok:
            return tok
    except Exception:
        pass

    # global legacy name (optional)
    try:
        from schwab_token_manager import fetch_schwab_token as global_fetch  # type: ignore
        tok = (global_fetch() or "").strip()
        if tok:
            return tok
    except Exception:
        pass

    raise RuntimeError(
        "Schwab token not found. "
        "Set env SCHWAB_ACCESS_TOKEN, or copy your old schwab_token_manager.py into "
        "trade_guardian/infra/schwab_token_manager_legacy.py."
    )
