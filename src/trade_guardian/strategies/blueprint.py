from typing import Optional, Dict, Any
from trade_guardian.domain.models import Blueprint, OrderLeg

# [辅助函数] 提取完整的 Quote 信息
def _extract_quote_full(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
    side_map = chain.get("callExpDateMap" if side == "CALL" else "putExpDateMap", {})
    for k in side_map:
        if k.startswith(exp):
            strikes = side_map[k]
            for s_key, quotes in strikes.items():
                if abs(float(s_key) - strike) < 0.01:
                    q = quotes[0]
                    # bid = float(q.get("bid", 0.0))
                    # ask = float(q.get("ask", 0.0))
                    # # 优先用 mark，没有则用 mid
                    # mark = float(q.get("mark") or (bid + ask) / 2.0)
                    bid = float(q.get("bid") or 0.0)
                    ask = float(q.get("ask") or 0.0)
                    mark = float(q.get("mark") or 0.0) or (bid+ask)/2

                    return {"bid": bid, "ask": ask, "mid": mark}
    # 没找到返回全0
    return {"bid": 0.0, "ask": 0.0, "mid": 0.0}

def _extract_mid_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Optional[float]:
    q = _extract_quote_full(chain, side, exp, strike)
    return q["mid"] if q["mid"] > 0 else None

def _extract_greeks_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
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

def build_diagonal_blueprint(
    *, symbol: str, underlying: float, chain: Dict[str, Any],
    short_exp: str, long_exp: str, target_short_strike: float, target_long_strike: float, side: str = "CALL",
) -> Optional[Blueprint]:
    
    short_mid = _extract_mid_for(chain, side, short_exp, target_short_strike)
    long_mid = _extract_mid_for(chain, side, long_exp, target_long_strike)

    est_debit = 0.0
    error_msg = None
    note = ""
    width = abs(target_short_strike - target_long_strike)

    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)
        if est_debit > width:
            excess = est_debit - width
            note = f"⚠️ High Debit (Net Risk: -${excess:.2f}). Vega Play."
        else:
            note = f"Healthy PMCC Setup. Width={width:.2f}"
    else:
        error_msg = "Missing pricing data"

    legs = [
        OrderLeg(symbol, "BUY", 1, long_exp, target_long_strike, side),
        OrderLeg(symbol, "SELL", 1, short_exp, target_short_strike, side)
    ]

    return Blueprint(
        symbol=symbol, strategy="DIAGONAL", legs=legs, est_debit=round(est_debit, 2),
        note=note, error=error_msg
    )

def build_straddle_blueprint(
    *, symbol: str, underlying: float, chain: Dict[str, Any], exp: str,
) -> Optional[Blueprint]:
    
    call_map = chain.get("callExpDateMap", {})
    target_key = None
    for k in call_map:
        if k.startswith(exp):
            target_key = k
            break
    if not target_key: 
        return Blueprint(symbol, "STRADDLE", [], 0.0, error="Expiry Not Found")
    
    strikes = sorted([float(s) for s in call_map[target_key].keys()])
    if not strikes:
        return Blueprint(symbol, "STRADDLE", [], 0.0, error="No Strikes")

    strike = min(strikes, key=lambda x: abs(x - underlying))
    
    # 获取双腿报价
    q_call = _extract_quote_full(chain, "CALL", exp, strike)
    q_put = _extract_quote_full(chain, "PUT", exp, strike)
    
    # [FIX] 严查流动性：任何一腿缺 Ask/Bid，视为不可交易
    # 防止 0 值导致 Spread 计算为 0% 从而绕过风控
    if q_call['ask'] <= 0 or q_put['ask'] <= 0 or q_call['bid'] <= 0 or q_put['bid'] <= 0:
        return Blueprint(
            symbol=symbol, strategy="STRADDLE", legs=[], est_debit=0.0, 
            error=f"Missing Quote Data for {strike} (Zero Liquidity)"
        )

    est_debit = q_call["mid"] + q_put["mid"]
    
    # 计算组合 Spread
    total_bid = q_call["bid"] + q_put["bid"]
    total_ask = q_call["ask"] + q_put["ask"]
    total_mid = (total_bid + total_ask) / 2.0
    
    spread_pct = 0.0
    if total_mid > 0:
        spread_pct = (total_ask - total_bid) / total_mid
    else:
        # Mid=0 也是异常
        return Blueprint(symbol, "STRADDLE", [], 0.0, error="Zero Mid Price")

    legs = [
        OrderLeg(symbol, "BUY", 1, exp, strike, "CALL"),
        OrderLeg(symbol, "BUY", 1, exp, strike, "PUT")
    ]

    bp = Blueprint(
        symbol=symbol, strategy="STRADDLE", legs=legs,
        est_debit=round(est_debit, 2), note="ATM Straddle"
    )
    
    # 将 Spread 存入 Meta，供 Gate 检查
    if not bp.meta: bp.meta = {}
    bp.meta["spread_pct"] = spread_pct
    
    return bp

def build_calendar_blueprint(
    *, symbol: str, underlying: float, chain: Dict[str, Any],
    short_exp: str, long_exp: str, prefer_side: str = "CALL"
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
        OrderLeg(symbol, "BUY", 1, long_exp, strike, prefer_side),
        OrderLeg(symbol, "SELL", 1, short_exp, strike, prefer_side)
    ]

    return Blueprint(
        symbol=symbol, strategy="CALENDAR", legs=legs,
        est_debit=round(est_debit, 2), note="ATM Calendar"
    )