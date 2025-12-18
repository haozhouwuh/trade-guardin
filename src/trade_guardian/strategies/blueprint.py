from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class CalendarBlueprint:
    symbol: str
    side: str  # "CALL" or "PUT"
    short_exp: str
    long_exp: str
    strike: float
    est_debit: Optional[float]  # calendar is typically debit
    short_mid: Optional[float]
    long_mid: Optional[float]
    note: str

    def one_liner(self) -> str:
        debit = f"{self.est_debit:.2f}" if isinstance(self.est_debit, (int, float)) else "N/A"
        return (
            f"{self.symbol} CAL({self.side})  "
            f"SELL {self.short_exp} {self.strike:g}  "
            f"BUY {self.long_exp} {self.strike:g}  "
            f"est_debit={debit}"
        )


def _nearest_strike(underlying: float, strikes: list[float]) -> Optional[float]:
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - underlying))


def _mid(bid: Optional[float], ask: Optional[float], last: Optional[float] = None) -> Optional[float]:
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    return None


def _extract_strikes(chain: Dict[str, Any], side: str, exp: str) -> list[float]:
    """
    Supports common shapes:
      - Schwab/TDA style: callExpDateMap / putExpDateMap : { "YYYY-MM-DD:DTE": { "strike": [contract] } }
      - Generic: chain["calls"][exp] = {strike: {...}} etc.
    Return float strikes list.
    """
    strikes: list[float] = []
    if not isinstance(chain, dict):
        return strikes

    if side.upper() == "CALL":
        root = chain.get("callExpDateMap")
    else:
        root = chain.get("putExpDateMap")

    # TDA style
    if isinstance(root, dict):
        for exp_key, strike_map in root.items():
            # exp_key example: "2025-12-23:6"
            if str(exp_key).startswith(exp):
                if isinstance(strike_map, dict):
                    for k in strike_map.keys():
                        try:
                            strikes.append(float(k))
                        except Exception:
                            pass
                break
        return sorted(set(strikes))

    # Generic fallback
    bucket = chain.get("calls" if side.upper() == "CALL" else "puts")
    if isinstance(bucket, dict):
        exp_map = bucket.get(exp)
        if isinstance(exp_map, dict):
            for k in exp_map.keys():
                try:
                    strikes.append(float(k))
                except Exception:
                    pass
    return sorted(set(strikes))


def _extract_mid_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Optional[float]:
    if not isinstance(chain, dict):
        return None

    root = chain.get("callExpDateMap") if side.upper() == "CALL" else chain.get("putExpDateMap")
    if isinstance(root, dict):
        for exp_key, strike_map in root.items():
            if str(exp_key).startswith(exp) and isinstance(strike_map, dict):
                leg = strike_map.get(f"{strike:g}") or strike_map.get(str(strike))
                # TDA: leg is list with single dict
                if isinstance(leg, list) and leg:
                    c = leg[0] if isinstance(leg[0], dict) else None
                elif isinstance(leg, dict):
                    c = leg
                else:
                    c = None
                if isinstance(c, dict):
                    return _mid(c.get("bid"), c.get("ask"), c.get("last"))
        return None

    # Generic fallback
    bucket = chain.get("calls" if side.upper() == "CALL" else "puts")
    if isinstance(bucket, dict):
        exp_map = bucket.get(exp)
        if isinstance(exp_map, dict):
            c = exp_map.get(f"{strike:g}") or exp_map.get(str(strike))
            if isinstance(c, dict):
                return _mid(c.get("bid"), c.get("ask"), c.get("last"))
    return None


def build_calendar_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    short_exp: str,
    long_exp: str,
    prefer_side: str = "CALL",
) -> Optional[CalendarBlueprint]:
    """
    Build ATM calendar blueprint:
      - Choose nearest strike by underlying using short expiry strikes universe
      - Use same strike for long expiry
      - Estimate debit = long_mid - short_mid
    """
    side = prefer_side.upper()
    strikes = _extract_strikes(chain, side=side, exp=short_exp)
    strike = _nearest_strike(underlying, strikes)
    if strike is None:
        return None

    short_mid = _extract_mid_for(chain, side=side, exp=short_exp, strike=strike)
    long_mid = _extract_mid_for(chain, side=side, exp=long_exp, strike=strike)

    est_debit = None
    note = "ATM strike chosen"
    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)

    if short_mid is None or long_mid is None:
        note = "missing bid/ask mid for one leg (check chain liquidity/shape)"

    return CalendarBlueprint(
        symbol=symbol,
        side=side,
        short_exp=short_exp,
        long_exp=long_exp,
        strike=float(strike),
        est_debit=est_debit,
        short_mid=short_mid,
        long_mid=long_mid,
        note=note,
    )
