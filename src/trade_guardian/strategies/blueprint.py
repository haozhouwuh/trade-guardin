from __future__ import annotations

from typing import Optional, Dict, Any

from trade_guardian.domain.models import Blueprint, OrderLeg


# =============================================================================
# Quote Helpers
# =============================================================================

def _extract_quote_full(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
    """
    Return {"bid": x, "ask": y, "mid": z}.
    - "mid" uses mark if valid (>0), else falls back to (bid+ask)/2 only if both bid/ask > 0.
    - If not found or insufficient data, returns zeros.
    """
    side_key = "callExpDateMap" if side.upper() == "CALL" else "putExpDateMap"
    side_map = chain.get(side_key, {}) or {}

    # Find the matching expiry bucket
    target_key = None
    for k in side_map.keys():
        if str(k).startswith(exp):
            target_key = k
            break
    if not target_key:
        return {"bid": 0.0, "ask": 0.0, "mid": 0.0}

    strikes_map = side_map.get(target_key, {}) or {}

    # Find the matching strike
    quote0 = None
    for s_key, quotes in strikes_map.items():
        try:
            if abs(float(s_key) - float(strike)) < 0.01 and quotes:
                quote0 = quotes[0]
                break
        except Exception:
            continue

    if not quote0:
        return {"bid": 0.0, "ask": 0.0, "mid": 0.0}

    bid = float(quote0.get("bid") or 0.0)
    ask = float(quote0.get("ask") or 0.0)
    mark = float(quote0.get("mark") or 0.0)

    # Hard mid fallback policy:
    # 1) mark if > 0
    # 2) (bid+ask)/2 only if both bid and ask are > 0
    mid = 0.0
    if mark > 0:
        mid = mark
    elif bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0

    return {"bid": bid, "ask": ask, "mid": float(mid)}


def _extract_mid_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Optional[float]:
    q = _extract_quote_full(chain=chain, side=side, exp=exp, strike=strike)
    return q["mid"] if q["mid"] > 0 else None


def _extract_greeks_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
    side_key = "callExpDateMap" if side.upper() == "CALL" else "putExpDateMap"
    exp_map = chain.get(side_key, {}) or {}

    target_key = None
    for k in exp_map.keys():
        if str(k).startswith(exp):
            target_key = k
            break
    if not target_key:
        return {}

    strikes_map = exp_map.get(target_key, {}) or {}
    quote0 = None
    for s_str, q_list in strikes_map.items():
        try:
            if abs(float(s_str) - float(strike)) < 0.01 and q_list:
                quote0 = q_list[0]
                break
        except Exception:
            continue

    if not quote0:
        return {}

    return {
        "delta": float(quote0.get("delta", 0.0) or 0.0),
        "gamma": float(quote0.get("gamma", 0.0) or 0.0),
        "theta": float(quote0.get("theta", 0.0) or 0.0),
    }


# =============================================================================
# Blueprint Builders
# =============================================================================

def build_diagonal_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    short_exp: str,
    long_exp: str,
    target_short_strike: float,
    target_long_strike: float,
    side: str = "CALL",
) -> Optional[Blueprint]:
    short_mid = _extract_mid_for(chain=chain, side=side, exp=short_exp, strike=target_short_strike)
    long_mid = _extract_mid_for(chain=chain, side=side, exp=long_exp, strike=target_long_strike)

    width = abs(float(target_short_strike) - float(target_long_strike))
    est_debit = 0.0
    error_msg: Optional[str] = None
    note = ""

    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)

        if est_debit > width and width > 0:
            excess = est_debit - width
            note = f"⚠️ High Debit (Net Risk: -${excess:.2f}). Vega Play."
        else:
            note = f"Healthy PMCC Setup. Width={width:.2f}"
    else:
        error_msg = "Missing pricing data"

    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=long_exp, strike=float(target_long_strike), type=side),
        OrderLeg(symbol=symbol, action="SELL", ratio=1, exp=short_exp, strike=float(target_short_strike), type=side),
    ]

    return Blueprint(
        symbol=symbol,
        strategy="DIAGONAL",
        legs=legs,
        est_debit=round(float(est_debit), 2),
        note=note,
        error=error_msg,
    )


def build_straddle_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    exp: str,
) -> Optional[Blueprint]:
    call_map = chain.get("callExpDateMap", {}) or {}

    target_key = None
    for k in call_map.keys():
        if str(k).startswith(exp):
            target_key = k
            break

    if not target_key:
        return Blueprint(symbol=symbol, strategy="STRADDLE", legs=[], est_debit=0.0, error="Expiry Not Found")

    strike_keys = list((call_map.get(target_key, {}) or {}).keys())
    strikes = []
    for s in strike_keys:
        try:
            strikes.append(float(s))
        except Exception:
            continue
    strikes.sort()

    if not strikes:
        return Blueprint(symbol=symbol, strategy="STRADDLE", legs=[], est_debit=0.0, error="No Strikes")

    strike = min(strikes, key=lambda x: abs(float(x) - float(underlying)))

    q_call = _extract_quote_full(chain=chain, side="CALL", exp=exp, strike=strike)
    q_put = _extract_quote_full(chain=chain, side="PUT", exp=exp, strike=strike)

    # Hard liquidity gate: any missing bid/ask => forbid (prevents 0% spread loophole)
    if q_call["bid"] <= 0 or q_call["ask"] <= 0 or q_put["bid"] <= 0 or q_put["ask"] <= 0:
        return Blueprint(
            symbol=symbol,
            strategy="STRADDLE",
            legs=[],
            est_debit=0.0,
            error=f"Missing Quote Data for {strike} (Zero Liquidity)",
        )

    # Hard mid policy: require both legs mid > 0 (mark or valid (bid+ask)/2)
    if q_call["mid"] <= 0 or q_put["mid"] <= 0:
        return Blueprint(
            symbol=symbol,
            strategy="STRADDLE",
            legs=[],
            est_debit=0.0,
            error=f"Zero Mid Price for {strike}",
        )

    est_debit = float(q_call["mid"] + q_put["mid"])

    total_bid = float(q_call["bid"] + q_put["bid"])
    total_ask = float(q_call["ask"] + q_put["ask"])
    total_mid = (total_bid + total_ask) / 2.0

    if total_mid <= 0:
        return Blueprint(symbol=symbol, strategy="STRADDLE", legs=[], est_debit=0.0, error="Zero Mid Price")

    spread_pct = (total_ask - total_bid) / total_mid

    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=exp, strike=float(strike), type="CALL"),
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=exp, strike=float(strike), type="PUT"),
    ]

    bp = Blueprint(
        symbol=symbol,
        strategy="STRADDLE",
        legs=legs,
        est_debit=round(est_debit, 2),
        note="ATM Straddle",
    )

    # meta is always present in models.py (default_factory), so just set it.
    bp.meta["spread_pct"] = float(spread_pct)
    bp.meta["strike"] = float(strike)
    bp.meta["exp"] = str(exp)
    bp.meta["q_call_bid"] = float(q_call["bid"])
    bp.meta["q_call_ask"] = float(q_call["ask"])
    bp.meta["q_put_bid"] = float(q_put["bid"])
    bp.meta["q_put_ask"] = float(q_put["ask"])

    return bp


def build_calendar_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    short_exp: str,
    long_exp: str,
    prefer_side: str = "CALL",
) -> Optional[Blueprint]:
    call_map = chain.get("callExpDateMap", {}) or {}

    target_key = None
    for k in call_map.keys():
        if str(k).startswith(short_exp):
            target_key = k
            break
    if not target_key:
        return None

    strike_keys = list((call_map.get(target_key, {}) or {}).keys())
    strikes = []
    for s in strike_keys:
        try:
            strikes.append(float(s))
        except Exception:
            continue
    strikes.sort()

    if not strikes:
        return None

    strike = min(strikes, key=lambda x: abs(float(x) - float(underlying)))

    short_mid = _extract_mid_for(chain=chain, side=prefer_side, exp=short_exp, strike=strike)
    long_mid = _extract_mid_for(chain=chain, side=prefer_side, exp=long_exp, strike=strike)

    est_debit = 0.0
    error_msg: Optional[str] = None
    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)
    else:
        error_msg = "Missing pricing data"

    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=long_exp, strike=float(strike), type=prefer_side),
        OrderLeg(symbol=symbol, action="SELL", ratio=1, exp=short_exp, strike=float(strike), type=prefer_side),
    ]

    return Blueprint(
        symbol=symbol,
        strategy="CALENDAR",
        legs=legs,
        est_debit=round(est_debit, 2),
        note="ATM Calendar",
        error=error_msg,
    )
