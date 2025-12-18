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
        # 在 Long Gamma 中，short_exp 指的是我们要买入的那个 Expiry (Active Expiry)
        # 为了兼容 ScanRow 数据结构，我们暂时借用 short_字段
        target_exp = str(tsf["short_exp"])
        target_dte = int(tsf["short_dte"])
        target_iv = float(tsf["short_iv"])
        hv_rank = float(ctx.hv.hv_rank)
        
        # Edge 定义反转：我们希望 HV > IV (市场低估波动)
        # Edge = HV / IV. 如果 > 1.0 说明做多波动率有利
        current_hv = float(ctx.hv.current_hv)
        edge = (current_hv / target_iv) if target_iv > 0 else 0.0

        # --- Scoring (0-100) ---
        bd = ScoreBreakdown(base=50)

        # 1. HV Rank (越低越好，买点便宜)
        if hv_rank <= 20:
            bd.hv = +15
        elif hv_rank <= 40:
            bd.hv = +5
        elif hv_rank >= 80:
            bd.hv = -15  # 太贵了，不做多
        elif hv_rank >= 60:
            bd.hv = -5

        # 2. Regime (喜欢 Contango，因为近月比远月便宜)
        if regime == "CONTANGO":
            bd.regime = +5
        elif regime == "BACKWARDATION":
            bd.regime = -10 # 倒挂说明近月已经被炒高了，贵
        
        # 3. Edge (HV vs IV)
        if edge > 1.1:
            bd.edge = +10
        elif edge < 0.9:
            bd.edge = -5

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        
        # --- Risk (0-100) ---
        # Long Gamma 的主要风险是 Theta (时间流逝) 和 IV Crush (买贵了)
        rbd = RiskBreakdown(base=20)

        # 1. Theta Risk (DTE 越短，Theta 损耗越快)
        # < 10天风险极高， 10-20 高， >30 适中
        if target_dte < 7:
            rbd.dte = +30
        elif target_dte < 14:
            rbd.dte = +20
        elif target_dte < 21:
            rbd.dte = +10
        
        # 2. Valuation Risk (买在高点)
        if hv_rank > 50:
            rbd.gamma = +int((hv_rank - 50) / 2) # IV Rank 越高，Risk 越高

        # 3. Regime Risk (Backwardation 意味着高成本)
        if regime == "BACKWARDATION":
            rbd.regime = +10

        risk = rbd.base + rbd.dte + rbd.gamma + rbd.regime

        # Tag 生成
        tag = "LG" # Long Gamma
        if regime == "CONTANGO": tag += "-C"
        if regime == "BACKWARDATION": tag += "-B"

        return ScanRow(
            symbol=ctx.symbol,
            price=float(ctx.price),
            short_exp=target_exp, # 借用字段
            short_dte=target_dte,
            short_iv=target_iv,
            base_iv=float(tsf["base_iv"]),
            edge=edge, # 注意：这里的 Edge 是 HV/IV
            hv_rank=hv_rank,
            regime=regime,
            curvature=curvature,
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=rbd,
        )

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        # 简单起见，暂不支持 Auto-Adjust (需要遍历不同 DTE)
        # 未来可以实现：如果当前 DTE 风险太高，自动向后找 DTE > 30 的
        return None, "-"