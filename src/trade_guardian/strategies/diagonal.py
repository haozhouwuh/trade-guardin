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
from trade_guardian.strategies.blueprint import build_diagonal_blueprint


class DiagonalStrategy(Strategy):
    """
    Strategy #6: Tactical Diagonal (Lightweight)
    
    Logic:
      - Long Leg: Uses the 'Month Anchor' (~30-45 DTE) found by Curve Analysis.
      - Short Leg: Uses the 'Short Anchor' (1-10 DTE).
      - Strikes: 
          Long @ ATM/ITM (High Vega, Low Delta Cost)
          Short @ OTM (Income)
      - Goal: Capture Term Structure Edge without the heavy capital of PMCC LEAPS.
    """
    name = "diagonal"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))
    
    def _find_strikes(self, ctx: Context) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """
        寻找轻量级 Diagonal 的行权价结构：
        Long: Month Exp @ First ITM or ATM
        Short: Short Exp @ First OTM
        """
        price = ctx.price
        chain = ctx.raw_chain
        tsf = ctx.tsf
        
        # 1. 锁定到期日 (直接复用 Curve 算法的结果)
        short_exp = tsf.get("short_exp")
        long_exp = tsf.get("month_exp") # 使用优化过的 35DTE 锚点，不再是 LEAPS

        if not short_exp or not long_exp:
            return None, None, None
        
        # 2. 获取 Strike Map
        call_map = chain.get("callExpDateMap", {})
        
        # 辅助函数：找特定日期的 strikes
        def get_strikes_for_exp(exp_date_str):
            # 模糊匹配日期前缀
            for k, v in call_map.items():
                if k.startswith(exp_date_str):
                    return sorted([float(s) for s in v.keys()])
            return []

        short_strikes = get_strikes_for_exp(short_exp)
        long_strikes = get_strikes_for_exp(long_exp)

        if not short_strikes or not long_strikes:
            return None, None, None

        # 3. 选择行权价 (Tactical Light Setup)
        
        # Short Leg: 卖出第一档虚值 (OTM)
        # 逻辑：找 > Price 的最小 Strike
        short_strike = next((s for s in short_strikes if s > price * 1.005), None)
        
        # Long Leg: 买入第一档实值 (ITM) 或 平值 (ATM)
        # 逻辑：找 <= Price 的最大 Strike
        # 这里用 <= Price 确保是 ITM 或 ATM，比 OTM 的 Short 腿低，构成正向对角
        long_strike_candidates = [s for s in long_strikes if s <= price]
        long_strike = long_strike_candidates[-1] if long_strike_candidates else long_strikes[0]

        # [保底逻辑] 确保 Long Strike < Short Strike (Call Diagonal 规则)
        if long_strike >= short_strike:
            # 尝试下移 Long Strike
            lower = [s for s in long_strikes if s < short_strike]
            if lower:
                long_strike = lower[-1]
            else:
                # 实在没空间 (比如股价正好卡在两个 Strike 中间)，做 Calendar (同价)
                # Calendar 也是一种特殊的 Diagonal
                long_strike = short_strike

        return short_strike, long_exp, long_strike

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        hv_rank = float(ctx.hv.hv_rank)
        price = float(ctx.price)
        
        # 基础数据
        short_iv = float(tsf.get("short_iv", 0))
        edge_month = float(tsf.get("edge_month", 0)) # 这是关键 Edge
        
        # 寻找结构
        short_strike, long_exp, long_strike = self._find_strikes(ctx)
        
        # --- Scoring ---
        bd = ScoreBreakdown(base=50)
        
        # Edge 驱动 (权重最大)
        bd.edge = int(self._clamp(edge_month * 80, -20, 40))
        
        # 结构分
        if short_strike and long_strike:
            bd.base += 10 # 成功构建结构奖励
        else:
            bd.penalties = -999 # 构建失败

        # 负 Edge 惩罚 (Backwardation 不适合做 Diagonal)
        if edge_month < 0:
            bd.regime = -30

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        
        # --- Risk ---
        # 复用 Long Gamma 的风险逻辑，因为轻量级 Diagonal 风险特征类似
        risk = 30
        if edge_month < 0: risk += 40
        
        tag = "DIAG"
        if edge_month > 0.15: tag += "+"

        row = ScanRow(
            symbol=ctx.symbol,
            price=price,
            short_exp=str(tsf.get("short_exp")),
            short_dte=int(tsf.get("short_dte", 0)),
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

        # 构建蓝图 (在此处生成，方便 Orchestrator 直接取用)
        if short_strike and long_strike:
            bp = build_diagonal_blueprint(
                symbol=ctx.symbol,
                underlying=price,
                chain=ctx.raw_chain,
                short_exp=str(tsf.get("short_exp")),
                long_exp=long_exp,
                target_short_strike=short_strike,
                target_long_strike=long_strike,
                side="CALL"
            )
            row.blueprint = bp
            
            # Meta 数据用于前端显示
            # Meta 数据用于前端显示
        row.meta = {
            "strategy": "diagonal",
            "short_strike": short_strike,
            "long_exp": long_exp,
            "long_strike": long_strike,
            "est_gamma": float(ctx.metrics.gamma) if ctx.metrics else 0.0,
            
            # 完整透传 TSF 数据
            "micro_exp": tsf.get("micro_exp"),
            "micro_dte": tsf.get("micro_dte"),
            "micro_iv": tsf.get("micro_iv"),
            "edge_micro": float(tsf.get("edge_micro", 0)), # <--- 补上了！
            
            "month_exp": tsf.get("month_exp"),
            "month_dte": tsf.get("month_dte"),
            "month_iv": tsf.get("month_iv"),
            "edge_month": edge_month,
        }
            
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        
        if row.cal_score < min_score:
            return None, "Score too low"
            
        if not row.blueprint:
            return None, "Structure build failed"

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