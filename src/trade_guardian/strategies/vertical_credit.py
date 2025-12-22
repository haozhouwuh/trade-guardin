from __future__ import annotations
from typing import Optional, Dict, Any, Tuple, List

from trade_guardian.domain.models import (
    Context, ScanRow, Blueprint, OrderLeg, ScoreBreakdown, RiskBreakdown, Recommendation
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

class VerticalCreditStrategy(Strategy):
    """
    Strategy #8: Vertical Credit Spread (Directional Income)
    
    Logic:
      - Directional selling of volatility.
      - Bull Put Spread (PCS): Sell OTM Put, Buy Lower Put. (Bullish/Neutral)
      - Bear Call Spread (CCS): Sell OTM Call, Buy Higher Call. (Bearish/Neutral)
      - Auto-Routing: Detects IV Skew. If Puts are richer (normal in stocks), defaults to PCS.
    
    Legs:
      - Short: ~30 Delta (More aggressive than IC's 20)
      - Long:  ~10 Delta (Wings)
    """
    name = "vertical_credit"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    def _find_strike_by_delta(self, chain: Dict, exp: str, side: str, target_delta: float) -> Optional[float]:
        map_key = "callExpDateMap" if side == "CALL" else "putExpDateMap"
        exp_map = chain.get(map_key, {})
        
        target_key = None
        for k in exp_map.keys():
            if k.startswith(exp):
                target_key = k
                break
        if not target_key: return None

        best_strike = None
        min_diff = 999.0
        
        strikes_data = exp_map[target_key]
        for s_str, quotes in strikes_data.items():
            try:
                strike = float(s_str)
                quote = quotes[0]
                d = abs(float(quote.get("delta", 0.0))) 
                if d < 0.01: continue 
                diff = abs(d - target_delta)
                if diff < min_diff:
                    min_diff = diff
                    best_strike = strike
            except: continue
        return best_strike

    def _get_quote_data(self, chain: Dict, exp: str, side: str, strike: float) -> Tuple[float, float]:
        map_key = "callExpDateMap" if side == "CALL" else "putExpDateMap"
        for k, v in chain.get(map_key, {}).items():
            if k.startswith(exp):
                strike_key = f"{strike:.1f}"
                found_quotes = None
                if strike_key in v:
                    found_quotes = v[strike_key]
                else:
                    for s_key, q_list in v.items():
                        if abs(float(s_key) - strike) < 0.01:
                            found_quotes = q_list
                            break
                if found_quotes:
                    q = found_quotes[0]
                    px = float(q.get("mark") or (float(q.get("bid",0)) + float(q.get("ask",0)))/2.0)
                    delta = float(q.get("delta", 0.0))
                    return px, delta
        return 0.0, 0.0

    def evaluate(self, ctx: Context) -> ScanRow:
        hv_rank = float(ctx.hv.hv_rank)
        
        # 1. 锁定时间 (Month Anchor)
        exp = ctx.tsf.get("month_exp")
        dte = int(ctx.tsf.get("month_dte", 0))
        
        if not exp or dte < 15:
            return self._empty_row(ctx, 0, 99, "DTE < 15")

        # 2. 决策方向：Bull Put (PCS) vs Bear Call (CCS)
        # 简单 Skew 判定：比较 25 Delta 的 Put 和 Call 谁更贵（IV更高）
        # 这里为了简化，我们默认做 Bull Put (PCS)，因为美股长期看涨且 Put 端溢价通常更高。
        # 如果需要 Bear Call，可以通过参数或逻辑扩展。
        # 暂定逻辑：默认 Bull Put (PCS)
        
        side_short = "PUT"
        side_long = "PUT"
        strat_tag = "PCS" # Put Credit Spread
        
        # 3. 寻找腿
        # Vertical 可以比 IC 稍微激进一点，Short Delta 选 0.25 - 0.30
        s_strike = self._find_strike_by_delta(ctx.raw_chain, exp, side_short, 0.30)
        l_strike = self._find_strike_by_delta(ctx.raw_chain, exp, side_long, 0.10)

        if not s_strike or not l_strike:
             return self._empty_row(ctx, 0, 99, "Legs Missing (Delta?)")

        # 逻辑检查
        price = ctx.price
        if side_short == "PUT":
            if not (l_strike < s_strike < price):
                 return self._empty_row(ctx, 0, 99, "Inv Strikes (PCS)")
        else: # CALL
             if not (price < s_strike < l_strike):
                 return self._empty_row(ctx, 0, 99, "Inv Strikes (CCS)")

        # 4. 计算价格
        p_s, d_s = self._get_quote_data(ctx.raw_chain, exp, side_short, s_strike)
        p_l, d_l = self._get_quote_data(ctx.raw_chain, exp, side_long, l_strike)
        
        credit = p_s - p_l
        width = abs(s_strike - l_strike)
        max_risk = width - credit
        if max_risk <= 0: max_risk = 0.1
        
        ror = credit / max_risk

        # 5. 评分
        score = 50
        if hv_rank > 50: score += 10
        if ror > 0.25: score += 15
        elif ror > 0.15: score += 5
        
        # 6. 构建结果
        legs = [
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_strike, side_short),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_strike, side_long)
        ]
        
        full_name = "BULL_PUT" if side_short == "PUT" else "BEAR_CALL"
        
        bp = Blueprint(
            symbol=ctx.symbol,
            strategy=full_name,
            legs=legs,
            est_debit= -round(credit, 2), # Negative Debit = Credit
            note=f"Credit ${credit:.2f} | Risk ${max_risk:.2f} | RoR {ror:.1%}",
            error=None,
            short_greeks={"delta": d_s}
        )

        calc_risk = max(0, 100 - score)

        row = ScanRow(
            symbol=ctx.symbol,
            price=ctx.price,
            short_exp=exp,
            short_dte=dte,
            short_iv=ctx.iv.current_iv,
            base_iv=ctx.tsf.get("month_iv", 0),
            edge=ctx.tsf.get("edge_month", 0),
            hv_rank=hv_rank,
            regime=str(ctx.tsf.get("regime")),
            curvature="NORMAL",
            tag=strat_tag,
            cal_score=int(score),
            short_risk=int(calc_risk),
            score_breakdown=ScoreBreakdown(base=50),
            risk_breakdown=RiskBreakdown(base=0),
            meta={"credit": credit, "width": width, "est_gamma": 0.0}
        )
        row.blueprint = bp
        return row

    def _empty_row(self, ctx, score, risk, note):
        # Fallback row to show failure in dashboard
        from trade_guardian.domain.models import ScanRow, Blueprint, ScoreBreakdown, RiskBreakdown
        bp = Blueprint(ctx.symbol, "VERT-FAIL", [], 0.0, error=note, note=note)
        return ScanRow(
            symbol=ctx.symbol,
            price=ctx.price,
            short_exp="N/A", short_dte=0, short_iv=0, base_iv=0, edge=0, hv_rank=0,
            regime="N/A", curvature="N/A", tag="VERT-FAIL",
            cal_score=score, short_risk=risk,
            score_breakdown=ScoreBreakdown(), risk_breakdown=RiskBreakdown(),
            meta={"error": note},
            blueprint=bp
        )
        
    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        if row.blueprint and not row.blueprint.error and row.cal_score >= min_score:
             rec = Recommendation(
                strategy="VERTICAL",
                symbol=ctx.symbol,
                action="OPEN",
                rationale=row.blueprint.note,
                entry_price=ctx.price,
                score=row.cal_score,
                conviction="MEDIUM",
                meta=row.meta
             )
             return rec, "OK"
        return None, "Score too low"