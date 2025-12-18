from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

# 导入子策略
from trade_guardian.strategies.calendar import CalendarStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy


class AutoStrategy(Strategy):
    """
    Strategy #5: Auto / Smart Router (v2 with Fuzzy Logic)
    Analyzes Vol Regime & HV Rank to dispatch to the best sub-strategy.
    
    Logic:
      - Low Vol (HV < 35): Force Long Gamma
      - High Vol (HV > 55) or Backwardation: Force Calendar (or future Iron Condor)
      - Mid Vol (35 <= HV <= 55): Run BOTH, pick the one with higher Score.
    """
    name = "auto"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        # 初始化所有可用的子策略
        self.calendar = CalendarStrategy(cfg, policy)
        self.long_gamma = LongGammaStrategy(cfg, policy)

    def evaluate(self, ctx: Context) -> ScanRow:
        """
        Routing Logic:
        1. Get HV Rank & Regime
        2. Decide Strategy (Deterministic or Competitive)
        3. Delegate evaluate()
        4. Tag result so Orchestrator knows which blueprint to build
        """
        hv_rank = float(ctx.hv.hv_rank)
        regime = str(ctx.tsf.get("regime", "FLAT"))

        # --- 决策逻辑 (Brain) ---
        
        # 1. [明确低波动] 且结构健康 -> 强制 Long Gamma
        # HV Rank < 35 且非倒挂，适合做多波动率
        if hv_rank < 35 and regime != "BACKWARDATION":
            row = self.long_gamma.evaluate(ctx)
            # Tag 示例: LG-C -> AUTO-LG-C
            row.tag = f"AUTO-{row.tag}"
            return row
        
        # 2. [明确高波动] 或 [倒挂] -> 强制 Calendar
        # HV Rank > 55 或 倒挂，做多波动率太贵，不如做时间价差
        elif hv_rank > 55 or regime == "BACKWARDATION":
            row = self.calendar.evaluate(ctx)
            # Tag 示例: C -> AUTO-C
            row.tag = f"AUTO-{row.tag}"
            return row

        # 3. [模糊地带 / 竞争区域] 35 <= HV <= 55
        # 两个策略都跑一遍，谁分高选谁
        else:
            res_lg = self.long_gamma.evaluate(ctx)
            res_cal = self.calendar.evaluate(ctx)

            # 比较分数 (Score)
            if res_lg.cal_score >= res_cal.cal_score:
                # Long Gamma 胜出 (例如 MSTR HV 36, LG=60分, Cal=28分)
                row = res_lg
                row.tag = f"AUTO-{row.tag}"
                return row
            else:
                # Calendar 胜出
                row = res_cal
                # 如果 Calendar 原 Tag 是 "C"，这里变为 "AUTO-C"
                # 为了区分，也可以写成 "AUTO-CAL-C"，但 Orchestrator 只要没看到 LG 就会默认处理
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
        
        # 2. Force Calendar
        elif hv_rank > 55 or regime == "BACKWARDATION":
            return self.calendar.recommend(ctx, min_score, max_risk)
        
        # 3. Competitive
        else:
            # 这里的比较比较麻烦，因为 recommend 返回的是 Recommendation 对象而不是分数
            # 简单起见，我们复用 evaluate 的结果来决定调用谁
            # 这是一个轻微的性能损耗，但在 CLI 模式下可忽略
            row_lg = self.long_gamma.evaluate(ctx)
            row_cal = self.calendar.evaluate(ctx)

            if row_lg.cal_score >= row_cal.cal_score:
                return self.long_gamma.recommend(ctx, min_score, max_risk)
            else:
                return self.calendar.recommend(ctx, min_score, max_risk)