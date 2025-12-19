from __future__ import annotations

from typing import Optional, Tuple, List, Dict, Any

from trade_guardian.domain.models import (
    Context,
    Recommendation,
    ScanRow,
    ScoreBreakdown,
    RiskBreakdown,
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy


class DiagonalStrategy(Strategy):
    """
    Strategy #6: Diagonal Spread / PMCC (Poor Man's Covered Call)
    
    Logic:
      - Buy Deep ITM LEAPS (Long Term) -> Substitute for Stock (High Delta)
      - Sell OTM Near Term Call -> Income Generation (High Theta)
      - Ideal for Bullish Long-term + Neutral/Bullish Short-term
      - Likes Contango (Long term IV < Short term IV)
    """
    name = "diagonal"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))
    
    def _find_strikes(self, ctx: Context, short_exp: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        price = ctx.price
        chain = ctx.raw_chain
        
        # ---------------------------------------------------------
        # 1. 寻找 Short Leg (卖方腿) - Near Term OTM Call
        # ---------------------------------------------------------
        call_map = chain.get("callExpDateMap", {})
        short_strikes_map = None
        for k in call_map.keys():
            if k.startswith(short_exp):
                short_strikes_map = call_map[k]
                break
        
        if not short_strikes_map: 
            return None, None, None

        avail_strikes = sorted([float(s) for s in short_strikes_map.keys()])
        
        # 找 First OTM，通常保留一点缓冲 (e.g. 1.01% - 1.02%)
        # [Safety] strictly ensure it is OTM. If price is 100, we want > 101.
        short_strike = next((s for s in avail_strikes if s > price * 1.015), None)
        
        # 如果找不到合适的 OTM，说明当前 Chain 都在实值或者数据缺失，不要强行选取
        if not short_strike: 
            return None, None, None

        # ---------------------------------------------------------
        # 2. 寻找 Long Leg (买方腿) - Deep ITM LEAPS
        # ---------------------------------------------------------
        long_candidates = []
        for exp_key in call_map.keys():
            parts = exp_key.split(":")
            exp_date = parts[0]
            try: 
                days = int(parts[1])
            except (IndexError, ValueError): 
                continue
            
            # LEAPS definition: usually > 1 year, but > 120 days is acceptable for diagonals
            if days > 120: 
                long_candidates.append((exp_date, days))
        
        if not long_candidates: 
            return None, None, None

        # 优先选 150-450 天 (Sweet spot for Theta decay curve vs Vega exposure)
        ideal = [c for c in long_candidates if 150 <= c[1] <= 450]
        if ideal:
            ideal.sort(key=lambda x: x[1])
            best_long_exp = ideal[0][0] # Pick the shortest valid LEAP to save Debit
            # best_long_days = ideal[0][1]
        else:
            long_candidates.sort(key=lambda x: x[1])
            best_long_exp = long_candidates[0][0]
            # best_long_days = long_candidates[0][1]
        
        long_strikes_map = None
        # 模糊匹配 Long Exp Key
        for k in call_map.keys():
            if k.startswith(best_long_exp):
                long_strikes_map = call_map[k]
                break
        
        if not long_strikes_map: 
            return None, None, None

        long_avail_strikes = sorted([float(s) for s in long_strikes_map.keys()])
        
        # [核心逻辑] Select Long Strike (Deep ITM)
        # Target: ~70-75 Delta substitute. Roughly Price * 0.70 to 0.75.
        target_strike = price * 0.70 
        
        # Find closest available strike
        long_strike = min(long_avail_strikes, key=lambda x: abs(x - target_strike))

        # [Safety Check] PMCC Golden Rule: Short Strike MUST be > Long Strike
        if long_strike >= short_strike:
            # Try to force a lower strike if available
            lower_candidates = [s for s in long_avail_strikes if s < short_strike]
            if lower_candidates:
                long_strike = lower_candidates[0] # Aggressively deep
            else:
                return None, None, None # Cannot form a diagonal

        return short_strike, best_long_exp, long_strike

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        regime = str(tsf["regime"])
        hv_rank = float(ctx.hv.hv_rank)
        price = float(ctx.price)
        
        # 获取 IV 数据
        short_iv = float(tsf["short_iv"])
        base_iv = float(tsf["base_iv"]) 
        
        # [新增] 1. 计算 Edge (Short IV vs Long IV)
        if base_iv > 0:
            raw_edge = (short_iv - base_iv) / base_iv
        else:
            raw_edge = 0.0

        # 寻找结构
        short_strike, long_exp, long_strike = self._find_strikes(ctx, str(tsf["short_exp"]))
        
        # --- Scoring (0-100) ---
        bd = ScoreBreakdown(base=50)

        # [修改] 连续打分逻辑
        edge_score = int(self._clamp(raw_edge * 100, -20, 25))
        bd.edge = edge_score

        hv_score = int((50 - hv_rank) / 3)
        bd.hv = int(self._clamp(hv_score, -15, 15))

        if regime == "CONTANGO": 
            bd.regime = +15 
        elif regime == "NORMAL": 
            bd.regime = +5
        elif regime == "BACKWARDATION": 
            bd.regime = -25 

        if not (short_strike and long_strike):
            bd.penalties = -999 
        
        # 负 Edge 惩罚
        if raw_edge < -0.05:
            bd.penalties -= 20

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties

        # --- Risk (Gamma Integration) ---
        rbd = RiskBreakdown(base=30)
        
        # [新增] 2. Gamma 风险估算 (这就是您要找的逻辑！)
        est_gamma = 0.0 
        try:
            # 简化的 Gamma 估算：用于捕捉 ONDS 这种短久期高风险票
            # 公式原理: Gamma ∝ 1 / (S * σ * √T)
            dte_years = max(1, int(tsf["short_dte"])) / 365.0
            if price > 0 and short_iv > 0:
                est_gamma = 0.4 / (price * short_iv * (dte_years ** 0.5))
        except:
            est_gamma = 0.0

        # Gamma 风险分档
        if est_gamma > 0.10: rbd.gamma = +35  # 高危 (如 ONDS)
        elif est_gamma > 0.05: rbd.gamma = +15
        elif est_gamma > 0.02: rbd.gamma = +5
        
        if regime == "BACKWARDATION": rbd.regime = +20

        risk = rbd.base + rbd.dte + rbd.gamma + rbd.regime

        tag = "PMCC"
        if raw_edge > 0.10: tag += "+" 

        row = ScanRow(
            symbol=ctx.symbol,
            price=price,
            short_exp=str(tsf["short_exp"]),
            short_dte=int(tsf["short_dte"]),
            short_iv=short_iv,
            base_iv=base_iv,
            edge=float(f"{raw_edge:.2f}"), # 显示真实 Edge
            hv_rank=hv_rank,
            regime=regime,
            curvature=str(tsf["curvature"]),
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=rbd,
        )

        if short_strike and long_strike:
            row.meta = {
                "strategy": "diagonal",
                "short_strike": short_strike,
                "long_exp": long_exp,
                "long_strike": long_strike,
                "spread_width": short_strike - long_strike,
                "est_gamma": float(f"{est_gamma:.4f}") # 方便调试
            }
            
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        
        if row.cal_score < min_score or row.short_risk > max_risk:
            return None, f"Score {row.cal_score} too low or Risk {row.short_risk} too high."
            
        if not row.meta or "long_strike" not in row.meta:
             return None, "No valid strike structure found."

        # Extract data from meta
        s_strike = row.meta["short_strike"]
        l_strike = row.meta["long_strike"]
        l_exp = row.meta["long_exp"]

        # Construct concise rationale
        rationale = (
            f"PMCC Setup: Buy {l_exp} {l_strike}C (ITM) / Sell {row.short_exp} {s_strike}C (OTM). "
            f"IV Rank {row.hv_rank:.1f} favors buying LEAPS. {row.regime} aids spread pricing."
        )

        rec = Recommendation(
            strategy=self.name,
            symbol=ctx.symbol,
            action="OPEN DIAGONAL",
            rationale=rationale,
            entry_price=ctx.price, # Reference only
            score=row.cal_score,
            conviction="MEDIUM" if row.cal_score < 75 else "HIGH",
            meta=row.meta
        )
        
        return rec, "OK"