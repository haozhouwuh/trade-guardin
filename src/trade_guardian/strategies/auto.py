from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

# [关键变更] 导入 Diagonal 而不是 Calendar
from trade_guardian.strategies.diagonal import DiagonalStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy


class AutoStrategy(Strategy):
    """
    Strategy #5: Auto / Smart Router (Final Version)
    Analyzes Vol Regime & HV Rank to dispatch to the best sub-strategy.
    
    Sub-Strategies:
      1. Long Gamma (Straddle): For Low Vol / Contango environments.
      2. Diagonal (PMCC): For Mid-High Vol / Structural plays.
    
    Logic:
      - Low Vol (HV < 35): Force Long Gamma
      - High Vol (HV > 55) or Backwardation: Force Diagonal
      - Mid Vol (35 <= HV <= 55): Run BOTH, pick the one with higher Score.
    """
    name = "auto"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        # 初始化双核引擎
        self.diagonal = DiagonalStrategy(cfg, policy)     # 替代了原来的 Calendar
        self.long_gamma = LongGammaStrategy(cfg, policy)

    def evaluate(self, ctx: Context) -> ScanRow:
        """
        Routing Logic:
        1. Get HV Rank & Regime
        2. Decide Strategy (Deterministic or Competitive)
        3. Delegate evaluate()
        4. Tag result with AUTO prefix
        """
        hv_rank = float(ctx.hv.hv_rank)
        regime = str(ctx.tsf.get("regime", "FLAT"))

        # --- 决策逻辑 (Brain) ---
        
        # 1. [明确低波动] 且结构健康 -> 倾向 Long Gamma
        # HV Rank < 35 且非倒挂，适合做多波动率
        if hv_rank < 35 and regime != "BACKWARDATION":
            row = self.long_gamma.evaluate(ctx)
            # Tag: LG-C -> AUTO-LG-C
            row.tag = f"AUTO-{row.tag}"
            return row
        
        # 2. [明确高波动] 或 [倒挂] -> 倾向 Diagonal (PMCC)
        # 此时买 Straddle 太贵，不如用 PMCC 降低成本或观望
        elif hv_rank > 55 or regime == "BACKWARDATION":
            row = self.diagonal.evaluate(ctx)
            # Tag: PMCC-C -> AUTO-PMCC-C
            row.tag = f"AUTO-{row.tag}"
            return row

        # 3. [模糊地带 / 竞争区域] 35 <= HV <= 55
        # 两个策略都跑一遍，谁分高选谁
        else:
            res_lg = self.long_gamma.evaluate(ctx)
            res_diag = self.diagonal.evaluate(ctx)

            # 比较分数 (Score)
            if res_lg.cal_score >= res_diag.cal_score:
                # Long Gamma 胜出
                row = res_lg
                row.tag = f"AUTO-{row.tag}"
                return row
            else:
                # Diagonal 胜出
                row = res_diag
                row.tag = f"AUTO-{row.tag}"
                return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        """
        Recommendation routing logic matches evaluate logic.
        """
        hv_rank = float(ctx.hv.hv_rank)
        regime = str(ctx.tsf.get("regime", "FLAT"))

        # 1. Force Long Gamma
        if hv_rank < 35 and regime != "BACKWARDATION":
            return self.long_gamma.recommend(ctx, min_score, max_risk)
        
        # 2. Force Diagonal
        elif hv_rank > 55 or regime == "BACKWARDATION":
            return self.diagonal.recommend(ctx, min_score, max_risk)
        
        # 3. Competitive
        else:
            # 复用 evaluate 的结果来决定调用谁
            row_lg = self.long_gamma.evaluate(ctx)
            row_diag = self.diagonal.evaluate(ctx)

            if row_lg.cal_score >= row_diag.cal_score:
                return self.long_gamma.recommend(ctx, min_score, max_risk)
            else:
                return self.diagonal.recommend(ctx, min_score, max_risk)