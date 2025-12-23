from __future__ import annotations
from typing import Optional, Dict, Any, Tuple, List

from trade_guardian.domain.models import (
    Context, ScanRow, Blueprint, OrderLeg, ScoreBreakdown, RiskBreakdown, Recommendation
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

class IronCondorStrategy(Strategy):
    """
    Strategy: Iron Condor (Flexible / Wide Wing Mode)
    
    Logic:
      - 默认模式改为 "Wide Wings" (即 Synthetic Strangle)，适配 IRA 账户。
      - Sell Legs: ~20 Delta (高胜率)
      - Buy Legs:  ~05 Delta (极远端保护，降低 Theta 损耗)
    """
    name = "iron_condor" # 保持原有注册名，上游无需修改

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        
        # [核心修改] 增加配置灵活性
        # 如果 cfg 里没有指定 wing_delta，默认使用 0.05 (宽翅膀/合成宽跨模式)
        # 如果想切回标准 IC，只需在 config 传参 wing_delta=0.10
        self.short_delta = self.cfg.get("short_delta", 0.20)
        # 默认就是您想要的 0.05 Delta (宽翅膀/退休账户模式)
        self.wing_delta = self.cfg.get("wing_delta", 0.05) 

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
        # [修改点] 使用 float('inf') 代表正无穷大，这是最标准的写法
        min_diff = float('inf') 
        
        strikes_data = exp_map[target_key]
        for s_str, quotes in strikes_data.items():
            try:
                strike = float(s_str)
                quote = quotes[0]
                d = abs(float(quote.get("delta", 0.0))) 
                
                if d < 0.001: continue 
                
                diff = abs(d - target_delta)
                
                # 任何实数 diff 都会小于 float('inf')，逻辑完美
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
        
        if not exp or dte < 20:
            return self._empty_row(ctx, score=0, risk=99, note="DTE < 20")

        # [使用配置参数]
        s_put_k = self._find_strike_by_delta(ctx.raw_chain, exp, "PUT", self.short_delta)
        l_put_k = self._find_strike_by_delta(ctx.raw_chain, exp, "PUT", self.wing_delta)
        
        s_call_k = self._find_strike_by_delta(ctx.raw_chain, exp, "CALL", self.short_delta)
        l_call_k = self._find_strike_by_delta(ctx.raw_chain, exp, "CALL", self.wing_delta)

        if not all([s_put_k, l_put_k, s_call_k, l_call_k]):
             return self._empty_row(ctx, score=0, risk=99, note="Legs Missing")
        
        price = ctx.price
        # 简单的逻辑检查
        if not (l_put_k < s_put_k < price < s_call_k < l_call_k):
             return self._empty_row(ctx, score=0, risk=99, note="Inv Strikes")

        p_sp, d_sp = self._get_quote_data(ctx.raw_chain, exp, "PUT", s_put_k)
        p_lp, d_lp = self._get_quote_data(ctx.raw_chain, exp, "PUT", l_put_k)
        p_sc, d_sc = self._get_quote_data(ctx.raw_chain, exp, "CALL", s_call_k)
        p_lc, d_lc = self._get_quote_data(ctx.raw_chain, exp, "CALL", l_call_k)
        
        credit = (p_sp - p_lp) + (p_sc - p_lc)
        width_put = s_put_k - l_put_k
        width_call = l_call_k - s_call_k
        max_width = max(width_put, width_call)
        
        max_risk = max_width - credit
        if max_risk <= 0: max_risk = 0.1
        
        ror = credit / max_risk

        # [自动适应评分]
        # 如果是宽翅膀 (Delta <= 0.05)，RoR 阈值自动降低
        # 如果是标准 IC (Delta >= 0.10)，RoR 阈值保持较高
        score = 50
        
        if hv_rank > 50: score += 10
        if hv_rank > 80: score += 10
        if hv_rank < 30: score -= 20
        
        if self.wing_delta <= 0.06:
            # === 宽翅膀评分标准 (IRA Mode) ===
            if ror > 0.18: score += 15
            elif ror > 0.12: score += 5
            elif ror < 0.10: score -= 15
        else:
            # === 标准 IC 评分标准 (Standard Mode) ===
            if ror > 0.30: score += 15
            elif ror > 0.20: score += 5
            elif ror < 0.15: score -= 10
        
        if ctx.tsf.get("regime") == "BACKWARDATION":
            score -= 30 
            
        # 根据模式打不同的 Tag，方便前台区分
        tag = "IC-WIDE" if self.wing_delta <= 0.06 else "IC-STD"
        if ror > (0.20 if self.wing_delta <= 0.06 else 0.35):
            tag += "-RICH"
        
        legs = [
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_put_k, "PUT"),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_put_k, "PUT"),
            OrderLeg(ctx.symbol, "SELL", 1, exp, s_call_k, "CALL"),
            OrderLeg(ctx.symbol, "BUY", 1, exp, l_call_k, "CALL"),
        ]
        
        bp = Blueprint(
            symbol=ctx.symbol,
            strategy="IRON_CONDOR", # 保持原有策略名，兼容数据库
            legs=legs,
            est_debit= -round(credit, 2),
            note=f"Wing D.{self.wing_delta:.2f} | Risk ${max_risk:.2f} | RoR {ror:.1%}",
            error=None
        )
        bp.short_greeks = {"delta": d_sc}

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
                "wing_delta": self.wing_delta
            }
        )
        row.blueprint = bp
        return row

    def _empty_row(self, ctx, score, risk, note):
        # 保持原有结构
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
            short_exp="N/A", short_dte=0, short_iv=0.0, base_iv=0.0, edge=0.0, 
            hv_rank=float(ctx.hv.hv_rank), regime="N/A", curvature="N/A", 
            tag="IC-FAIL", cal_score=score, short_risk=risk,
            score_breakdown=ScoreBreakdown(), risk_breakdown=RiskBreakdown(),
            meta={"error": note}, blueprint=bp
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
        return None, "Score too low"