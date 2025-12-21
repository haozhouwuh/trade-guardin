from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy
from trade_guardian.strategies.diagonal import DiagonalStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy


class AutoStrategy(Strategy):
    """
    Strategy #5: Auto / Smart Router (Brain V5 - Structure First)
    
    Philosophy:
      - Structure Trumps Volatility: If the Term Structure offers a good edge (>0.20),
        we prioritize Diagonal spreads to harvest Theta/Vega differential, even if IV is low.
      - Low Vol Fallback: Only when structure is flat do we revert to Long Gamma (Straddle)
        to play for pure expansion.
    
    Routing Order:
      1. Backwardation -> LG (Defense)
      2. Edge > 0.20   -> DIAG (Attack Structure)
      3. HV < 30       -> LG (Attack Vol Floor)
      4. Default       -> LG
    """
    name = "auto"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        self.diagonal = DiagonalStrategy(cfg, policy)
        self.long_gamma = LongGammaStrategy(cfg, policy)

    def evaluate(self, ctx: Context) -> ScanRow:
        hv_rank = float(ctx.hv.hv_rank)
        tsf = ctx.tsf
        regime = str(tsf.get("regime", "FLAT"))
        edge_month = float(tsf.get("edge_month", 0.0))

        # --- 决策逻辑 (Brain V5) ---
        
        # 1. [倒挂保护] Backwardation -> 强制 Long Gamma
        # 这种时候卖近端是自杀，必须防守
        if regime == "BACKWARDATION":
            row = self.long_gamma.evaluate(ctx)
            row.tag = f"AUTO-LG"
            return row

        # 2. [结构优先] 只要 Edge 足够好 (> 0.20)，优先做 Diagonal
        # 即使 HV 很低，优秀的结构也能提供比 Straddle 更好的盈亏比
        # (包含了 > 0.35 的超级结构情况)
        if edge_month >= 0.20:
            row_diag = self.diagonal.evaluate(ctx)
            # 只有构建成功才返回，否则掉下去走兜底
            if row_diag and row_diag.meta and "long_strike" in row_diag.meta:
                row_diag.tag = f"AUTO-DIAG" 
                return row_diag

        # 3. [低波博弈] 结构平庸，但波动率在地板 -> 强制 Long Gamma
        # NVDA (Edge 0.16) 会落到这里
        if hv_rank < 30:
            row = self.long_gamma.evaluate(ctx)
            row.tag = f"AUTO-LG" 
            return row
        
        # 4. [默认兜底] 结构平坦且波动率中等 -> Long Gamma
        row = self.long_gamma.evaluate(ctx)
        row.tag = f"AUTO-LG"
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        if "DIAG" in row.tag:
            return self.diagonal.recommend(ctx, min_score, max_risk)
        else:
            return self.long_gamma.recommend(ctx, min_score, max_risk)