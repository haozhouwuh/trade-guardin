from typing import Optional, Dict, Any
from trade_guardian.domain.models import Blueprint, OrderLeg

# [辅助函数] 保持不变
def _extract_greeks_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
    """尝试从原始数据中提取 Delta/Gamma/Theta"""
    side_map_key = "callExpDateMap" if side.upper() == "CALL" else "putExpDateMap"
    exp_map = chain.get(side_map_key, {})
    
    target_key = None
    for k in exp_map.keys():
        if k.startswith(exp):
            target_key = k
            break
    
    if not target_key: return {}
    
    strikes = exp_map[target_key]
    quote = None
    strike_str = f"{strike:.1f}"
    if strike_str in strikes:
        quote = strikes[strike_str][0]
    else:
        for s_str, q_list in strikes.items():
            if abs(float(s_str) - strike) < 0.01:
                quote = q_list[0]
                break
    
    if not quote: return {}

    return {
        "delta": float(quote.get("delta", 0.0)),
        "gamma": float(quote.get("gamma", 0.0)),
        "theta": float(quote.get("theta", 0.0))
    }

def _extract_mid_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Optional[float]:
    side_map = chain.get("callExpDateMap" if side == "CALL" else "putExpDateMap", {})
    for k in side_map:
        if k.startswith(exp):
            strikes = side_map[k]
            for s_key, quotes in strikes.items():
                if abs(float(s_key) - strike) < 0.01:
                    q = quotes[0]
                    if "mark" in q: return float(q["mark"])
                    if "bid" in q and "ask" in q: return (float(q["bid"]) + float(q["ask"])) / 2.0
    return None

# =========================================================
# 核心修改：废弃私有 Blueprint 类，统一返回 domain.models.Blueprint
# =========================================================

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
    
    short_mid = _extract_mid_for(chain, side=side, exp=short_exp, strike=target_short_strike)
    long_mid = _extract_mid_for(chain, side=side, exp=long_exp, strike=target_long_strike)

    est_debit = 0.0
    error_msg = None
    note = ""
    width = abs(target_short_strike - target_long_strike)

    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)
        
        # 宽容的风控逻辑：只记录 Note，不报错
        if est_debit > width:
            excess = est_debit - width
            note = f"⚠️ High Debit (Net Risk: -${excess:.2f}). Vega Play."
        else:
            note = f"Healthy PMCC Setup. Width={width:.2f}"
    else:
        # 价格缺失
        error_msg = "Missing pricing data"

    # 构造标准 Legs
    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=long_exp, strike=target_long_strike, type=side),
        OrderLeg(symbol=symbol, action="SELL", ratio=1, exp=short_exp, strike=target_short_strike, type=side)
    ]

    return Blueprint(
        symbol=symbol,
        strategy="DIAGONAL",
        legs=legs,
        est_debit=round(est_debit, 2),
        note=note,
        error=error_msg
    )

def build_straddle_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    exp: str,
) -> Optional[Blueprint]:
    
    # 查找 ATM Strike
    call_map = chain.get("callExpDateMap", {})
    target_key = None
    for k in call_map:
        if k.startswith(exp):
            target_key = k
            break
    if not target_key: return None
    
    strikes = sorted([float(s) for s in call_map[target_key].keys()])
    strike = min(strikes, key=lambda x: abs(x - underlying))
    
    call_mid = _extract_mid_for(chain, "CALL", exp, strike)
    put_mid = _extract_mid_for(chain, "PUT", exp, strike)
    
    est_debit = 0.0
    if call_mid and put_mid:
        est_debit = call_mid + put_mid
        
    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=exp, strike=strike, type="CALL"),
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=exp, strike=strike, type="PUT")
    ]

    return Blueprint(
        symbol=symbol,
        strategy="STRADDLE",
        legs=legs,
        est_debit=round(est_debit, 2),
        note="ATM Straddle"
    )

def build_calendar_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    short_exp: str,
    long_exp: str,
    prefer_side: str = "CALL"
) -> Optional[Blueprint]:
    
    call_map = chain.get("callExpDateMap", {})
    target_key = None
    for k in call_map:
        if k.startswith(short_exp):
            target_key = k
            break
    if not target_key: return None
    strikes = sorted([float(s) for s in call_map[target_key].keys()])
    strike = min(strikes, key=lambda x: abs(x - underlying))
    
    short_mid = _extract_mid_for(chain, prefer_side, short_exp, strike)
    long_mid = _extract_mid_for(chain, prefer_side, long_exp, strike)
    
    est_debit = 0.0
    if short_mid and long_mid:
        est_debit = long_mid - short_mid
        
    legs = [
        OrderLeg(symbol=symbol, action="BUY", ratio=1, exp=long_exp, strike=strike, type=prefer_side),
        OrderLeg(symbol=symbol, action="SELL", ratio=1, exp=short_exp, strike=strike, type=prefer_side)
    ]

    return Blueprint(
        symbol=symbol,
        strategy="CALENDAR",
        legs=legs,
        est_debit=round(est_debit, 2),
        note="ATM Calendar"
    )