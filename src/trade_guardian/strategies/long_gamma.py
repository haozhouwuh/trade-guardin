from __future__ import annotations

from trade_guardian.domain.models import (
    Context,
    ScanRow,
    ScoreBreakdown,
    RiskBreakdown,
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy


class LongGammaStrategy(Strategy):
    """
    Strategy: Long Gamma / Straddle
    Logic: Buy cheap volatility, avoiding high Gamma risk zones.
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
        if target_iv > 0:
            raw_edge = (current_hv - target_iv) / target_iv
        else:
            raw_edge = 0.0

        # --- Scoring (0-100) ---
        bd = ScoreBreakdown(base=50)

        # 1. HV Rank (低位适合买入)
        if hv_rank <= 20: bd.hv = +15
        elif hv_rank <= 40: bd.hv = +5
        elif hv_rank >= 80: bd.hv = -20 
        elif hv_rank >= 60: bd.hv = -10

        # 2. Regime
        if regime == "CONTANGO": bd.regime = +5
        elif regime == "BACKWARDATION": bd.regime = -15 
        
        # 3. Edge Impact
        if raw_edge > 0.1: bd.edge = +10
        elif raw_edge < -0.1: bd.edge = -10

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        
        # --- Risk (Gamma Integrated) ---
        rbd = RiskBreakdown(base=20)

        # 1. Theta Risk
        if target_dte < 7: rbd.dte = +30
        elif target_dte < 14: rbd.dte = +20
        elif target_dte < 21: rbd.dte = +10
        
        # 2. Gamma Risk Calculation (Total Position)
        est_gamma = 0.0
        if ctx.metrics and hasattr(ctx.metrics, 'gamma'):
            # [Fix] 这里的 Gamma 是单腿的，Straddle 有两条腿，所以风险 x2
            est_gamma = float(ctx.metrics.gamma) * 2.0
        else:
            # Fallback estimation (公式本身就是估算 Straddle 的，所以不用乘2)
            try:
                vol_decimal = target_iv / 100.0 if target_iv > 2.0 else target_iv
                dte_years = max(1, target_dte) / 365.0
                if price > 0 and vol_decimal > 0:
                    est_gamma = 0.8 / (price * vol_decimal * (dte_years ** 0.5))
            except:
                est_gamma = 0.0

        # [Calibration] Total Gamma Thresholds
        # 0.20+ : EXTREME (e.g. ONDS) -> +50 Risk
        # 0.12+ : HIGH    (e.g. TSLL, TQQQ) -> +30 Risk
        # 0.08+ : ELEVATED (e.g. SOXL) -> +15 Risk
        
        if est_gamma >= 0.20:
            rbd.gamma = +50
        elif est_gamma >= 0.12:
            rbd.gamma = +30
        elif est_gamma >= 0.08:
            rbd.gamma = +15
        
        # 3. Regime Risk
        if regime == "BACKWARDATION": rbd.regime = +10
        
        # 4. Valuation Risk
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
        
        row.meta = {
            "strategy": "long_gamma",
            "est_gamma": float(f"{est_gamma:.4f}"),
            "raw_edge": raw_edge
        }

        return row