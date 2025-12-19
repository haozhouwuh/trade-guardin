from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import (
    Context,
    Recommendation,
    ScanRow,
    ScoreBreakdown,
    RiskBreakdown,
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy


class LongGammaStrategy(Strategy):
    """
    Strategy #4: Long Gamma / Straddle Scanner
    Logic: Buy Volatility when it is cheap.
    """
    name = "long_gamma"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        regime = str(tsf["regime"])
        curvature = str(tsf["curvature"])
        
        target_exp = str(tsf["short_exp"])
        target_dte = int(tsf["short_dte"])
        target_iv = float(tsf["short_iv"])
        hv_rank = float(ctx.hv.hv_rank)
        price = float(ctx.price)
        current_hv = float(ctx.hv.current_hv)

        # [Edge Calculation]
        # Long Gamma 获利前提是 HV (实际波动) > IV (隐含波动)
        # 结果为正数表示“IV被低估”，值得买入
        if target_iv > 0:
            raw_edge = (current_hv - target_iv) / target_iv
        else:
            raw_edge = 0.0

        # --- Scoring (0-100) ---
        bd = ScoreBreakdown(base=50)

        # 1. HV Rank (越低越好)
        if hv_rank <= 20: bd.hv = +15
        elif hv_rank <= 40: bd.hv = +5
        elif hv_rank >= 80: bd.hv = -20 
        elif hv_rank >= 60: bd.hv = -10

        # 2. Regime
        if regime == "CONTANGO": bd.regime = +5
        elif regime == "BACKWARDATION": bd.regime = -15 
        
        # 3. Edge
        if raw_edge > 0.1: bd.edge = +10
        elif raw_edge < -0.1: bd.edge = -10

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        
        # --- Risk (Gamma Integrated) ---
        rbd = RiskBreakdown(base=20)

        # 1. Theta Risk
        if target_dte < 7: rbd.dte = +30
        elif target_dte < 14: rbd.dte = +20
        elif target_dte < 21: rbd.dte = +10
        
        # 2. Gamma Risk 计算
        est_gamma = 0.0
        try:
            # [Fix] IV 单位标准化：如果 IV > 2.0 (比如 112.0)，说明是百分数，需要除以 100 转小数
            vol_decimal = target_iv / 100.0 if target_iv > 2.0 else target_iv
            
            # DTE 转化为年
            dte_years = max(1, target_dte) / 365.0
            
            if price > 0 and vol_decimal > 0:
                # Straddle Gamma ≈ 0.8 / (S * σ * √T)
                # 0.8 是经验系数 (单腿 ATM Gamma * 2)
                est_gamma = 0.8 / (price * vol_decimal * (dte_years ** 0.5))
        except Exception:
            est_gamma = 0.0

        # Risk Mapping
        # Gamma 越高，意味着 Theta 损耗越剧烈，风险越高
        if est_gamma > 0.15: rbd.gamma = +35 
        elif est_gamma > 0.08: rbd.gamma = +20
        elif est_gamma > 0.04: rbd.gamma = +10

        # 3. Regime Risk
        if regime == "BACKWARDATION": rbd.regime = +10
        
        # 4. Valuation Risk (High Rank = Mean Reversion Risk)
        if hv_rank > 60: rbd.regime += 10

        risk = rbd.base + rbd.dte + rbd.gamma + rbd.regime

        tag = "LG" 
        if regime == "CONTANGO": tag += "-C"
        
        row = ScanRow(
            symbol=ctx.symbol,
            price=price,
            short_exp=target_exp, 
            short_dte=target_dte,
            short_iv=target_iv,
            base_iv=float(tsf["base_iv"]),
            edge=float(f"{raw_edge:.2f}"), 
            hv_rank=hv_rank,
            regime=regime,
            curvature=curvature,
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=rbd,
        )
        
        # 注入 Meta 数据，供 Orchestrator 使用
        row.meta = {
            "strategy": "long_gamma",
            "est_gamma": float(f"{est_gamma:.4f}"),
            "raw_edge": raw_edge
        }

        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        # 暂不支持 Recommendation 直接输出，主要用于 Scanlist
        return None, "-"