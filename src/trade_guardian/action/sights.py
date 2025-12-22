from typing import List, Tuple, Optional
import math

def find_nearest_strike(price: float, strikes: List[float]) -> float:
    """找到离当前价格最近的 Strike (ATM)"""
    if not strikes:
        return 0.0
    return min(strikes, key=lambda x: abs(x - price))

def get_strike_step(price: float) -> float:
    """估算 Strike 步长 (用于判断漂移是否显著)"""
    if price < 50: return 0.5
    if price < 100: return 1.0
    if price < 200: return 2.5 # NVDA/BABA 这种级别
    if price < 500: return 5.0
    return 10.0

def recenter_target(
    current_price: float, 
    proposed_strike: float, 
    available_strikes: List[float],
    strategy_type: str = "STRADDLE"
) -> Tuple[float, bool]:
    """
    判断是否需要重新瞄准
    Returns: (NewStrike, IsChanged)
    """
    if not available_strikes:
        return proposed_strike, False

    # 1. 确定目标 Strike
    # 对于 Straddle/Calendar/IronFly，我们永远追求绝对 ATM
    target_strike = find_nearest_strike(current_price, available_strikes)
    
    # 2. 判断是否发生显著漂移
    # 只有当目标 Strike 确实改变了，才触发变更
    if abs(target_strike - proposed_strike) > 0.01:
        return target_strike, True
        
    return proposed_strike, False