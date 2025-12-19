from dataclasses import dataclass
from typing import Optional, Dict, Any

# [辅助函数升级] 尝试提取 Greeks
def _extract_greeks_for(chain: Dict[str, Any], side: str, exp: str, strike: float) -> Dict[str, float]:
    """尝试从原始数据中提取 Delta/Gamma/Theta"""
    # 注意：这取决于你的数据源格式。
    # 假设 chain['callExpDateMap'][exp][strike][0] 里面除了 mark 还有 delta/gamma 等字段
    # 如果没有，这个函数会返回空字典，不影响程序运行
    
    side_map_key = "callExpDateMap" if side.upper() == "CALL" else "putExpDateMap"
    exp_map = chain.get(side_map_key, {})
    
    # 尝试找到对应的 Expiry Key (模糊匹配 "2026-01-02:...")
    target_key = None
    for k in exp_map.keys():
        if k.startswith(exp):
            target_key = k
            break
    
    if not target_key: return {}
    
    strikes = exp_map[target_key]
    # 寻找 Strike (key 是 string)
    # 浮点数匹配比较麻烦，我们做个简单的转 string 尝试
    # 或者遍历
    quote = None
    strike_str = f"{strike:.1f}"
    if strike_str in strikes:
        quote = strikes[strike_str][0]
    else:
        # 尝试 float 匹配
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
    # ... (保持原有的 _extract_mid_for 代码不变) ...
    # 为了完整性，这里简略，请保留你原文件里的逻辑
    side_map = chain.get("callExpDateMap" if side == "CALL" else "putExpDateMap", {})
    # ... (原有逻辑)
    # 这里只是占位，实际请不要删除原来的逻辑
    for k in side_map:
        if k.startswith(exp):
            strikes = side_map[k]
            for s_key, quotes in strikes.items():
                if abs(float(s_key) - strike) < 0.01:
                    q = quotes[0]
                    # 优先用 mark，没有则用 (bid+ask)/2
                    if "mark" in q: return float(q["mark"])
                    if "bid" in q and "ask" in q: return (float(q["bid"]) + float(q["ask"])) / 2.0
    return None


@dataclass
class CalendarBlueprint:
    symbol: str
    strike: float
    short_exp: str
    long_exp: str
    est_debit: Optional[float]
    note: str
    # 新增
    short_greeks: Optional[Dict[str, float]] = None
    long_greeks: Optional[Dict[str, float]] = None

    def one_liner(self) -> str:
        debit = f"{self.est_debit:.2f}" if isinstance(self.est_debit, (int, float)) else "N/A"
        return (
            f"{self.symbol} CALENDAR  "
            f"-{self.short_exp} / +{self.long_exp} @ {self.strike:g}  "
            f"est_debit={debit}"
        )

@dataclass
class StraddleBlueprint:
    symbol: str
    strike: float
    exp: str
    est_debit: Optional[float]
    note: str
    # 新增
    greeks: Optional[Dict[str, float]] = None # ATM 的 Greeks

    def one_liner(self) -> str:
        debit = f"{self.est_debit:.2f}" if isinstance(self.est_debit, (int, float)) else "N/A"
        return (
            f"{self.symbol} STRADDLE  "
            f"BUY {self.exp} {self.strike:g} CALL/PUT  "
            f"est_debit={debit}"
        )

@dataclass
class DiagonalBlueprint:
    symbol: str
    side: str
    short_exp: str
    short_strike: float
    long_exp: str
    long_strike: float
    est_debit: Optional[float]
    width: float
    max_loss: Optional[float]
    note: str
    # 新增
    short_greeks: Optional[Dict[str, float]] = None
    long_greeks: Optional[Dict[str, float]] = None

    def one_liner(self) -> str:
        debit = f"{self.est_debit:.2f}" if isinstance(self.est_debit, (int, float)) else "N/A"
        return (
            f"{self.symbol} DIAGONAL({self.side})  "
            f"BUY {self.long_exp} {self.long_strike:g} / "
            f"SELL {self.short_exp} {self.short_strike:g}  "
            f"est_debit={debit}"
        )

# === 更新 Build 函数 ===

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
) -> Optional[DiagonalBlueprint]:
    
    short_mid = _extract_mid_for(chain, side=side, exp=short_exp, strike=target_short_strike)
    long_mid = _extract_mid_for(chain, side=side, exp=long_exp, strike=target_long_strike)

    # [新增] 提取 Greeks
    short_greeks = _extract_greeks_for(chain, side, short_exp, target_short_strike)
    long_greeks = _extract_greeks_for(chain, side, long_exp, target_long_strike)

    est_debit = None
    note = ""
    width = abs(target_short_strike - target_long_strike)

    if isinstance(short_mid, (int, float)) and isinstance(long_mid, (int, float)):
        est_debit = float(long_mid - short_mid)
        
        # Hard Filter
        if est_debit > width:
            return None 
        else:
            note = f"Healthy PMCC Setup. Width={width:.2f}"
    else:
        return None

    return DiagonalBlueprint(
        symbol=symbol,
        side=side,
        short_exp=short_exp,
        short_strike=target_short_strike,
        long_exp=long_exp,
        long_strike=target_long_strike,
        est_debit=est_debit,
        width=width,
        max_loss=est_debit,
        note=note,
        short_greeks=short_greeks,
        long_greeks=long_greeks
    )

def build_straddle_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    exp: str,
) -> Optional[StraddleBlueprint]:
    
    # 这里的逻辑稍微简化，我们只取 Call 的 Greeks 做参考
    # 实际上 Straddle 需要 Call 和 Put 的 Greeks 只有 Gamma 是相加，Delta 是中性
    
    # 1. Find ATM strike (保持原有逻辑)
    call_map = chain.get("callExpDateMap", {})
    # ... (省略查找 ATM Strike 逻辑，保持你原有的)
    # 假设我们找到了 strike
    
    # 为了演示，我把原逻辑简化写在这里，你需要保留你原来的 find strike 逻辑
    target_key = None
    for k in call_map:
        if k.startswith(exp):
            target_key = k
            break
    if not target_key: return None
    
    strikes = sorted([float(s) for s in call_map[target_key].keys()])
    strike = min(strikes, key=lambda x: abs(x - underlying))
    
    # 获取价格
    call_mid = _extract_mid_for(chain, "CALL", exp, strike)
    put_mid = _extract_mid_for(chain, "PUT", exp, strike)
    
    # [新增] 获取 Greeks
    atm_greeks = _extract_greeks_for(chain, "CALL", exp, strike)
    
    est_debit = None
    if call_mid and put_mid:
        est_debit = call_mid + put_mid
        
    return StraddleBlueprint(
        symbol=symbol,
        strike=strike,
        exp=exp,
        est_debit=est_debit,
        note="ATM strike chosen",
        greeks=atm_greeks
    )

def build_calendar_blueprint(
    *,
    symbol: str,
    underlying: float,
    chain: Dict[str, Any],
    short_exp: str,
    long_exp: str,
    prefer_side: str = "CALL"
) -> Optional[CalendarBlueprint]:
    
    # ... (保留原有 ATM Strike 查找逻辑)
    # 假设找到 strike
    
    # 模拟查找
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
    
    # [新增] Greeks
    short_greeks = _extract_greeks_for(chain, prefer_side, short_exp, strike)
    long_greeks = _extract_greeks_for(chain, prefer_side, long_exp, strike)
    
    est_debit = None
    if short_mid and long_mid:
        est_debit = long_mid - short_mid
        
    return CalendarBlueprint(
        symbol=symbol,
        strike=strike,
        short_exp=short_exp,
        long_exp=long_exp,
        est_debit=est_debit,
        note="ATM Calendar",
        short_greeks=short_greeks,
        long_greeks=long_greeks
    )