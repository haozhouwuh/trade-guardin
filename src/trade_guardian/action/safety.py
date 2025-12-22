from typing import Dict, Tuple

class SafetyCheckResult:
    def __init__(self, passed: bool, reason: str, mid: float = 0.0, spread: float = 0.0):
        self.passed = passed
        self.reason = reason
        self.mid = mid
        self.spread = spread

def check_liquidity(quote: Dict, strict_mode: bool = True) -> SafetyCheckResult:
    """
    检查期权报价的流动性健康度
    quote 结构: {'bid': 3.30, 'ask': 3.35, 'mark': 3.325}
    """
    bid = float(quote.get('bid', 0.0))
    ask = float(quote.get('ask', 0.0))
    
    if bid <= 0 or ask <= 0:
        return SafetyCheckResult(False, "Zero Liquidity (Bid/Ask is 0)")
    
    mid = (bid + ask) / 2.0
    spread = ask - bid
    
    # 1. 绝对值保护：防止错价 (Crossed Market)
    if bid > ask:
        return SafetyCheckResult(False, f"Crossed Market (Bid {bid} > Ask {ask})", mid, spread)
        
    # 2. Spread 比例检查
    # 对于高价期权 (> $1.0)，Spread 不应超过 Mid 的 15% (Strict) 或 25% (Loose)
    # 对于低价期权 (< $1.0)，允许更宽的比例，但限制绝对值
    spread_ratio = spread / mid
    
    threshold = 0.15 if strict_mode else 0.25
    
    # 针对低价票的豁免 (e.g. Bid 0.05 Ask 0.10, ratio=66% 但其实正常)
    if mid < 0.50:
        threshold = 0.50 
    elif mid < 1.0:
        threshold = 0.30

    if spread_ratio > threshold:
        return SafetyCheckResult(
            False, 
            f"Spread Too Wide: {spread:.2f} ({spread_ratio:.1%}) > {threshold:.0%}", 
            mid, 
            spread
        )
        
    return SafetyCheckResult(True, "OK", mid, spread)