from __future__ import annotations
from trade_guardian.domain.models import Context, ScanRow, ScoreBreakdown, RiskBreakdown
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

class LongGammaStrategy(Strategy):
    name = "long_gamma"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        symbol = ctx.symbol
        
        # 1. 提取三点数据
        short_iv = float(tsf.get("short_iv", 0.0))
        edge_micro = float(tsf.get("edge_micro", 0.0))
        edge_month = float(tsf.get("edge_month", 0.0))
        
        # 2. 评分逻辑 (双因子驱动)
        score = 60
        # Micro Edge: 短期必须便宜 (权重 40%)
        score += int(self._clamp(edge_micro * 40, -20, 20))
        # Month Edge: 结构必须支撑 (权重 60%)
        score += int(self._clamp(edge_month * 60, -20, 30))
        
        score = self._clamp(score, 0, 100)

        # 3. 构造 Gate 信号 (三段式风控)
        gate = "WAIT"
        dna = "QUIET" # 稍后 Orchestrator 会更新动能
        
        # 基础门槛
        if score > 75 and edge_micro > 0.05 and edge_month > 0.10:
            gate = "READY" # 介于 WAIT 和 EXEC 之间，等待动能触发
        
        if symbol in ["TSLL", "TQQQ", "SOXL", "ONDS", "SMCI", "IWM"]:
            gate = "FORBID"

        # 4. 构造 Tag
        tag = "LG"
        if edge_micro > 0.15: tag += "-M" # Micro 极好
        if edge_month > 0.30: tag += "-K" # Month 极好 (K=Structure)

        # 5. 返回 ScanRow (meta 携带全量双基准数据)
        bd = ScoreBreakdown(base=60) 
        rbd = RiskBreakdown(base=20)
        
        row = ScanRow(
            symbol=symbol,
            price=float(ctx.price),
            short_exp=tsf.get("short_exp", ""),
            short_dte=int(tsf.get("short_dte", 0)),
            short_iv=short_iv,
            base_iv=tsf.get("month_iv", 0.0), # 兼容老字段
            edge=edge_month,                 # 兼容老字段
            hv_rank=50.0,
            regime="NORMAL",
            curvature="FLAT",
            tag=tag,
            cal_score=int(score),
            short_risk=20,
            score_breakdown=bd,
            risk_breakdown=rbd,
        )
        
        # 将双基准放入 meta，供 UI 渲染
        row.meta = {
            "micro_exp": tsf.get("micro_exp"),
            "micro_dte": tsf.get("micro_dte"),
            "micro_iv": tsf.get("micro_iv"),
            "edge_micro": edge_micro,
            
            "month_exp": tsf.get("month_exp"),
            "month_dte": tsf.get("month_dte"),
            "month_iv": tsf.get("month_iv"),
            "edge_month": edge_month,
            
            "est_gamma": float(ctx.metrics.gamma) * 2.0 if ctx.metrics else 0.0,
            "strike": round(float(ctx.price), 1)
        }
        return row