from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy
from trade_guardian.strategies.diagonal import DiagonalStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy
from trade_guardian.strategies.vertical_credit import VerticalCreditStrategy

# [NEW] 定义杠杆/高波 ETF 列表
LEV_ETFS = ["TQQQ", "SQQQ", "SOXL", "SOXS", "TSLL", "TSLS", "NVDL", "LABU", "UVXY"]

class AutoStrategy(Strategy):
    """
    Strategy #5: Auto / Smart Router (Brain V7.1 - Strict LevETF Priority)
    
    Routing Order (Corrected):
      1. Backwardation -> LG (Defense)
      2. LevETF        -> VERTICAL (Force Gamma Neutrality)  <-- PRIORITY UP
      3. Edge > 0.20   -> DIAG (Attack Structure)
      4. High Volatility -> VERTICAL (Harvest Premium)
      5. Default       -> LG
    """
    name = "auto"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        self.diagonal = DiagonalStrategy(cfg, policy)
        self.long_gamma = LongGammaStrategy(cfg, policy)
        self.vertical = VerticalCreditStrategy(cfg, policy)

    def evaluate(self, ctx: Context) -> ScanRow:
        hv_rank = float(ctx.hv.hv_rank)
        tsf = ctx.tsf
        regime = str(tsf.get("regime", "FLAT"))
        edge_month = float(tsf.get("edge_month", 0.0))
        current_iv = float(ctx.iv.current_iv)
        is_lev_etf = ctx.symbol in LEV_ETFS

        # --- 决策逻辑 (Brain V7.1) ---
        
        # 1. [倒挂保护] Backwardation -> 强制 Long Gamma (防守)
        if regime == "BACKWARDATION":
            row = self.long_gamma.evaluate(ctx)
            # [MOD] 移除 AUTO- 前缀，标记为 DEFENSE
            row.tag = "LG-DEFENSE"
            return row

        # 2. [杠杆降维打击] 只要是杠杆 ETF，强制走 Vertical
        # 这一步必须在 Diagonal 之前，防止 TQQQ 被拉去做对角线
        if is_lev_etf:
            row_vert = self.vertical.evaluate(ctx)
            if row_vert and "FAIL" not in (row_vert.tag or ""):
                # [MOD] 直接返回 Vertical 策略生成的 Tag (如 BULL-PUT)
                return row_vert

        # 3. [结构优先] Edge > 0.20 -> Diagonal (进攻)
        # 非杠杆 ETF，且结构好，做对角线
        if edge_month >= 0.20:
            row_diag = self.diagonal.evaluate(ctx)
            if row_diag and row_diag.meta and "long_strike" in row_diag.meta:
                # [MOD] 移除 AUTO- 前缀，直接使用 DIAG 本身的 Tag
                return row_diag

        # 4. [高波收租] HV Rank > 30 OR IV > 40% -> Vertical
        if hv_rank > 30 or current_iv > 40.0:
            row_vert = self.vertical.evaluate(ctx)
            if row_vert and "FAIL" not in (row_vert.tag or ""):
                # [MOD] 直接返回 Vertical 策略生成的 Tag
                return row_vert

        # 5. [低波博弈] HV < 30 -> Long Gamma
        if hv_rank < 30:
            row = self.long_gamma.evaluate(ctx)
            # [MOD] 标记这是低波主要玩法
            row.tag = "LG-LOWVOL" 
            return row
        
        # 6. [默认兜底] -> Long Gamma
        row = self.long_gamma.evaluate(ctx)
        row.tag = "LG-BASE"
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        tag = row.tag or ""
        
        # [MOD] 增加对 BULL/BEAR 等新 Tag 的支持
        if "DIAG" in tag:
            return self.diagonal.recommend(ctx, min_score, max_risk)
        elif "PCS" in tag or "CCS" in tag or "VERT" in tag or "BULL" in tag or "BEAR" in tag:
            return self.vertical.recommend(ctx, min_score, max_risk)
        else:
            return self.long_gamma.recommend(ctx, min_score, max_risk)