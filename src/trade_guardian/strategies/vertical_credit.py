from __future__ import annotations
from typing import Optional, Dict, Any, Tuple, List

from trade_guardian.domain.models import (
    Context, ScanRow, Blueprint, OrderLeg, ScoreBreakdown, RiskBreakdown, Recommendation
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

class VerticalCreditStrategy(Strategy):
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
        exp = ctx.tsf.get("month_exp")
        dte = int(ctx.tsf.get("month_dte", 0))
        
        if not exp or dte < 15:
            return self._empty_row(ctx, 0, 99, "DTE < 15")

        side_short = "PUT"
        side_long = "PUT"
        strat_tag = "BULL-PUT" if side_short == "PUT" else "BEAR-CALL"
        
        s_strike = self._find_strike_by_delta(ctx.raw_chain, exp, side_short, 0.30)
        l_strike = self._find_strike_by_delta(ctx.raw_chain, exp, side_long, 0.10)

        if not s_strike or not l_strike:
             return self._empty_row(ctx, 0, 99, "Legs Missing (Delta?)")

        price = ctx.price
        if side_short == "PUT":
            if not (l_strike < s_strike < price):
                 return self._empty_row(ctx, 0, 99, "Inv Strikes (PCS)")
        else:
             if not (price < s_strike < l_strike):
                 return self._empty_row(ctx, 0, 99, "Inv Strikes (CCS)")

        p_s, d_s = self._get_quote_data(ctx.raw_chain, exp, side_short, s_strike)
        p_l, d_l = self._get_quote_data(ctx.raw_chain, exp, side_long, l_strike)
        
        credit = p_s - p_l
        width = abs(s_strike - l_strike)
        max_risk = width - credit
        if max_risk <= 0: max_risk = 0.1
        
        ror = credit / max_risk
        score = 50
        if hv_rank > 50: score += 10
        if ror > 0.25: score += 15
        elif ror > 0.15: score += 5
        
        if score >= 70: strat_tag += "★"
        
        legs = [
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_strike, side_short),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_strike, side_long)
        ]
        
        bp = Blueprint(
            symbol=ctx.symbol,
            strategy=strat_tag.replace("★", ""),
            legs=legs,
            est_debit= -round(credit, 2),
            note=f"Credit ${credit:.2f} | Risk ${max_risk:.2f} | RoR {ror:.1%}",
            error=None,
            short_greeks={"delta": d_s}
        )

        calc_risk = max(0, 100 - score)
        meta_data = ctx.tsf.copy() if ctx.tsf else {}
        meta_data.update({"credit": credit, "width": width, "est_gamma": 0.0})

        # [FIX] 还原为 TSF 锚点
        tsf_short_exp = str(ctx.tsf.get("short_exp", "N/A"))
        tsf_short_dte = int(ctx.tsf.get("short_dte", 0))
        tsf_short_iv = float(ctx.tsf.get("short_iv", 0.0))

        row = ScanRow(
            symbol=ctx.symbol,
            price=ctx.price,
            short_exp=tsf_short_exp,
            short_dte=tsf_short_dte,
            short_iv=tsf_short_iv,
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
            meta=meta_data 
        )
        row.blueprint = bp
        return row

    def _empty_row(self, ctx, score, risk, note):
        from trade_guardian.domain.models import ScanRow, Blueprint, ScoreBreakdown, RiskBreakdown
        bp = Blueprint(ctx.symbol, "VERT-FAIL", [], 0.0, error=note, note=note)
        meta_data = ctx.tsf.copy() if ctx.tsf else {}
        meta_data["error"] = note
        return ScanRow(
            symbol=ctx.symbol, price=ctx.price,
            short_exp="N/A", short_dte=0, short_iv=0, base_iv=0, edge=0, hv_rank=0,
            regime="N/A", curvature="N/A", tag="VERT-FAIL",
            cal_score=score, short_risk=risk,
            score_breakdown=ScoreBreakdown(), risk_breakdown=RiskBreakdown(),
            meta=meta_data, blueprint=bp
        )
        
    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        if row.blueprint and not row.blueprint.error and row.cal_score >= min_score:
             rec = Recommendation(
                strategy="VERTICAL", symbol=ctx.symbol, action="OPEN",
                rationale=row.blueprint.note, entry_price=ctx.price,
                score=row.cal_score, conviction="MEDIUM", meta=row.meta
             )
             return rec, "OK"
        return None, "Score too low"