from __future__ import annotations

from typing import Optional, Tuple, List, Dict, Any

from trade_guardian.domain.models import (
    Context, Recommendation, ScanRow, ScoreBreakdown, RiskBreakdown, Blueprint
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy
from trade_guardian.strategies.blueprint import build_diagonal_blueprint


class DiagonalStrategy(Strategy):
    name = "diagonal"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        
        # [NEW] 读取 Diagonal 专用的短腿限制
        diag_cfg = cfg.get("strategies", {}).get("diagonal", {})
        self.short_min_dte = diag_cfg.get("short_min_dte", 1)
        self.short_max_dte = diag_cfg.get("short_max_dte", 14)
        self.use_rank_0 = diag_cfg.get("use_rank_0_for_short", False)

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))
    
    def _find_strikes_with_exp(self, ctx: Context, short_exp: str, long_exp: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        price = ctx.price
        chain = ctx.raw_chain
        
        if not short_exp or not long_exp: return None, None, None
        
        call_map = chain.get("callExpDateMap", {})
        
        def get_strikes_for_exp(exp_date_str):
            for k, v in call_map.items():
                if k.startswith(exp_date_str):
                    return sorted([float(s) for s in v.keys()])
            return []

        short_strikes = get_strikes_for_exp(short_exp)
        long_strikes = get_strikes_for_exp(long_exp)

        if not short_strikes or not long_strikes: return None, None, None

        short_strike = next((s for s in short_strikes if s > price * 1.005), None)
        long_strike_candidates = [s for s in long_strikes if s <= price]
        long_strike = long_strike_candidates[-1] if long_strike_candidates else long_strikes[0]

        if long_strike >= short_strike:
            lower = [s for s in long_strikes if s < short_strike]
            if lower:
                long_strike = lower[-1]
            else:
                long_strike = short_strike

        return short_strike, long_exp, long_strike

    def evaluate(self, ctx: Context) -> ScanRow:
        # [FIX] 关键修正：创建副本，绝不修改原始 ctx.tsf
        tsf = ctx.tsf.copy()
        
        hv_rank = float(ctx.hv.hv_rank)
        price = float(ctx.price)
        
        short_exp = str(tsf.get("short_exp"))
        short_dte = int(tsf.get("short_dte", 0))
        short_iv = float(tsf.get("short_iv", 0.0))
        
        # [FIX] 如果配置允许且 Rank 0 合法，覆盖本地 TSF 使用最近点
        nearest_dte = int(tsf.get("nearest_dte", 999))
        
        if self.use_rank_0 and nearest_dte < short_dte and nearest_dte >= self.short_min_dte:
            short_exp = str(tsf.get("nearest_exp"))
            short_dte = nearest_dte
            short_iv = float(tsf.get("nearest_iv", 0.0))
            
            # 重新计算 Edge
            base_iv = float(tsf.get("month_iv", 0.0))
            denom = max(12.0, base_iv) # 12% 底线
            edge_month = (base_iv - short_iv) / denom
            
            # 更新本地副本
            tsf["short_exp"] = short_exp
            tsf["short_dte"] = short_dte
            tsf["short_iv"] = short_iv
            tsf["edge_month"] = edge_month
        
        edge_month = float(tsf.get("edge_month", 0.0))
        
        # 寻找结构
        short_strike, long_exp, long_strike = self._find_strikes_with_exp(ctx, short_exp, str(tsf.get("month_exp")))
        
        # --- Scoring ---
        bd = ScoreBreakdown(base=50)
        bd.edge = int(self._clamp(edge_month * 80, -20, 40))
        
        if short_strike and long_strike:
            bd.base += 10 
        else:
            bd.penalties = -999 

        if edge_month < 0: bd.regime = -30

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        risk = 30
        if edge_month < 0: risk += 40
        
        tag = "DIAG"
        if edge_month > 0.15: tag += "+"

        row = ScanRow(
            symbol=ctx.symbol,
            price=price,
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=short_iv,
            base_iv=float(tsf.get("month_iv", 0)),
            edge=edge_month,
            hv_rank=hv_rank,
            regime=str(tsf.get("regime")),
            curvature=str(tsf.get("curvature")),
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=RiskBreakdown(base=risk),
        )

        if short_strike and long_strike:
            bp = build_diagonal_blueprint(
                symbol=ctx.symbol,
                underlying=price,
                chain=ctx.raw_chain,
                short_exp=short_exp,
                long_exp=long_exp,
                target_short_strike=short_strike,
                target_long_strike=long_strike,
                side="CALL"
            )
            row.blueprint = bp
            
            row.meta = {
                "strategy": "diagonal",
                "short_strike": short_strike,
                "long_exp": long_exp,
                "long_strike": long_strike,
                "est_gamma": float(ctx.metrics.gamma) if ctx.metrics else 0.0,
                "micro_exp": tsf.get("micro_exp"),
                "micro_dte": tsf.get("micro_dte"),
                "micro_iv": tsf.get("micro_iv"),
                "edge_micro": float(tsf.get("edge_micro", 0)),
                "month_exp": tsf.get("month_exp"),
                "month_dte": tsf.get("month_dte"),
                "month_iv": tsf.get("month_iv"),
                "edge_month": edge_month,
            }
            
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        if row.cal_score < min_score: return None, "Score too low"
        if not row.blueprint: return None, "Structure build failed"

        rec = Recommendation(
            strategy="DIAGONAL",
            symbol=ctx.symbol,
            action="OPEN DIAGONAL",
            rationale=f"Tactical Diagonal: Edge {row.edge:.2f}",
            entry_price=ctx.price,
            score=row.cal_score,
            conviction="HIGH",
            meta=row.meta
        )
        return rec, "OK"