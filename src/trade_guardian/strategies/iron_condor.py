from __future__ import annotations
from typing import Optional, Dict, Any, Tuple, List

from trade_guardian.domain.models import (
    Context, ScanRow, Blueprint, OrderLeg, ScoreBreakdown, RiskBreakdown, Recommendation
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

class IronCondorStrategy(Strategy):
    """
    Strategy #7: Iron Condor (High Probability / Range Bound)
    
    Logic:
      - 核心：Delta 布局。利用高 IV 时的期权高溢价，卖出 OTM 宽跨式，并买入更远端保护。
      - 适用环境：High HV Rank (>50), Structure is FLAT/CONTANGO.
      - 标准腿 (Standard Setup):
        - Sell ~20 Delta Call / Sell ~20 Delta Put (Short Strangle)
        - Buy ~10 Delta Call  / Buy ~10 Delta Put  (Protection Wings)
    """
    name = "iron_condor"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    def _find_strike_by_delta(self, chain: Dict, exp: str, side: str, target_delta: float) -> Optional[float]:
        """
        [核心算法] 在指定到期日寻找 Delta 绝对值最接近 target 的 Strike
        """
        map_key = "callExpDateMap" if side == "CALL" else "putExpDateMap"
        exp_map = chain.get(map_key, {})
        
        # 1. 模糊匹配 Expiry Key (e.g. "2025-01-16:29")
        target_key = None
        for k in exp_map.keys():
            if k.startswith(exp):
                target_key = k
                break
        if not target_key: return None

        # 2. 遍历 Strike 寻找最佳 Delta
        best_strike = None
        min_diff = 999.0
        
        strikes_data = exp_map[target_key]
        for s_str, quotes in strikes_data.items():
            try:
                strike = float(s_str)
                quote = quotes[0]
                # 注意：Put Delta 是负数，取绝对值比较
                d = abs(float(quote.get("delta", 0.0))) 
                
                # 过滤无效数据 (Delta 太小可能是深虚值垃圾数据)
                if d < 0.01: continue 
                
                diff = abs(d - target_delta)
                if diff < min_diff:
                    min_diff = diff
                    best_strike = strike
            except: continue
            
        return best_strike

    def _get_quote_data(self, chain: Dict, exp: str, side: str, strike: float) -> Tuple[float, float]:
        """返回 (Price, Delta)"""
        map_key = "callExpDateMap" if side == "CALL" else "putExpDateMap"
        for k, v in chain.get(map_key, {}).items():
            # 尝试精确匹配 Strike
            if k.startswith(exp):
                strike_key = f"{strike:.1f}" # Schwab key format often 1 decimal
                # 模糊查找 key
                found_quotes = None
                if strike_key in v:
                    found_quotes = v[strike_key]
                else:
                    # float loop lookup
                    for s_key, q_list in v.items():
                        if abs(float(s_key) - strike) < 0.01:
                            found_quotes = q_list
                            break
                
                if found_quotes:
                    q = found_quotes[0]
                    # 价格优先用 mark，没有则中间价
                    px = float(q.get("mark") or (float(q.get("bid",0)) + float(q.get("ask",0)))/2.0)
                    delta = float(q.get("delta", 0.0))
                    return px, delta
        return 0.0, 0.0

    def evaluate(self, ctx: Context) -> ScanRow:
        hv_rank = float(ctx.hv.hv_rank)
        
        # 1. 锁定时间：使用 Month Anchor (通常 30-45 DTE)
        exp = ctx.tsf.get("month_exp")
        dte = int(ctx.tsf.get("month_dte", 0))
        
        if not exp or dte < 20:
            return self._empty_row(ctx, score=0, risk=99, note="DTE < 20")

        # 2. 寻找四条腿 (Delta 寻址)
        # Put Side (Otm)
        s_put_k = self._find_strike_by_delta(ctx.raw_chain, exp, "PUT", 0.20)
        l_put_k = self._find_strike_by_delta(ctx.raw_chain, exp, "PUT", 0.10)
        
        # Call Side (Otm)
        s_call_k = self._find_strike_by_delta(ctx.raw_chain, exp, "CALL", 0.20)
        l_call_k = self._find_strike_by_delta(ctx.raw_chain, exp, "CALL", 0.10)

        # [DEBUG关键点] 完整性检查：如果任意一条腿没找到 (通常是因为周末 Delta=0)，返回带错误信息的 Row
        if not all([s_put_k, l_put_k, s_call_k, l_call_k]):
             return self._empty_row(ctx, score=0, risk=99, note="Legs Missing (Delta=0?)")
        
        # 逻辑检查：Put Long < Short < Price < Call Short < Call Long
        price = ctx.price
        if not (l_put_k < s_put_k < price < s_call_k < l_call_k):
             return self._empty_row(ctx, score=0, risk=99, note="Inv Strikes")

        # 3. 估算价格与风险
        p_sp, d_sp = self._get_quote_data(ctx.raw_chain, exp, "PUT", s_put_k)
        p_lp, d_lp = self._get_quote_data(ctx.raw_chain, exp, "PUT", l_put_k)
        p_sc, d_sc = self._get_quote_data(ctx.raw_chain, exp, "CALL", s_call_k)
        p_lc, d_lc = self._get_quote_data(ctx.raw_chain, exp, "CALL", l_call_k)
        
        credit = (p_sp - p_lp) + (p_sc - p_lc)
        width_put = s_put_k - l_put_k
        width_call = l_call_k - s_call_k
        max_width = max(width_put, width_call)
        
        max_risk = max_width - credit
        if max_risk <= 0: max_risk = 0.1 # 防止除零
        
        ror = credit / max_risk

        # 4. 评分逻辑 (Scoring)
        score = 50
        
        if hv_rank > 50: score += 10
        if hv_rank > 80: score += 10
        if hv_rank < 30: score -= 20
        
        if ror > 0.30: score += 15
        elif ror > 0.20: score += 5
        elif ror < 0.15: score -= 10
        
        if ctx.tsf.get("regime") == "BACKWARDATION":
            score -= 30 
            
        # 5. 构建 Blueprint
        tag = "IC"
        if ror > 0.35: tag = "IC-RICH"
        
        legs = [
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_put_k, "PUT"),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_put_k, "PUT"),
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_call_k, "CALL"),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_call_k, "CALL"),
        ]
        
        bp = Blueprint(
            symbol=ctx.symbol,
            strategy="IRON_CONDOR",
            legs=legs,
            est_debit= -round(credit, 2), # 负数 Debit = Credit
            note=f"Credit ${credit:.2f} | Risk ${max_risk:.2f} | RoR {ror:.1%}",
            error=None
        )
        
        bp.short_greeks = {"delta": d_sc}

        # Risk 分数简单映射
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
            tag=tag,
            cal_score=int(score),
            short_risk=int(calc_risk),
            score_breakdown=ScoreBreakdown(base=50),
            risk_breakdown=RiskBreakdown(base=0),
            meta={
                "credit": credit, 
                "width": max_width,
                "est_gamma": 0.0
            }
        )
        row.blueprint = bp
        return row

    def _empty_row(self, ctx, score, risk, note):
        """
        返回一个表示失败的 ScanRow，而不是 None。
        这样 Orchestrator 就不会静默跳过它，前台能看到 "IC-FAIL"。
        """
        # 构造一个带 Error 的 Blueprint
        bp = Blueprint(
            symbol=ctx.symbol,
            strategy="IC-FAIL",
            legs=[],
            est_debit=0.0,
            error=note,
            note=note
        )

        return ScanRow(
            symbol=ctx.symbol,
            price=ctx.price,
            short_exp="N/A", 
            short_dte=0, 
            short_iv=0.0, 
            base_iv=0.0, 
            edge=0.0, 
            hv_rank=float(ctx.hv.hv_rank),
            regime="N/A", 
            curvature="N/A", 
            tag="IC-FAIL",
            cal_score=score, 
            short_risk=risk,
            score_breakdown=ScoreBreakdown(), 
            risk_breakdown=RiskBreakdown(),
            meta={"error": note},
            blueprint=bp
        )

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        row = self.evaluate(ctx)
        if row.blueprint and not row.blueprint.error and row.cal_score >= min_score:
             rec = Recommendation(
                strategy="IRON_CONDOR",
                symbol=ctx.symbol,
                action="OPEN",
                rationale=row.blueprint.note,
                entry_price=ctx.price,
                score=row.cal_score,
                conviction="MEDIUM",
                meta=row.meta
             )
             return rec, "OK"
        return None, "Score too low or build fail"