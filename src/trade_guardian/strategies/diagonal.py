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


class DiagonalStrategy(Strategy):
    """
    Strategy #6: Diagonal Spread / PMCC (Poor Man's Covered Call)
    
    Logic:
      - Buy Deep ITM LEAPS (Long Term) -> Substitute for Stock
      - Sell OTM Near Term Call -> Income Generation
      - Ideal for Bullish Long-term + Neutral/Bullish Short-term
      - Likes Contango (Long term IV is lower relative to Short term)
    """
    name = "diagonal"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))
    
    def _find_strikes(self, ctx: Context, short_exp: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        price = ctx.price
        chain = ctx.raw_chain
        
        # ---------------------------------------------------------
        # 1. 寻找 Short Leg (卖方腿)
        # ---------------------------------------------------------
        call_map = chain.get("callExpDateMap", {})
        short_strikes_map = None
        for k in call_map.keys():
            if k.startswith(short_exp):
                short_strikes_map = call_map[k]
                break
        
        if not short_strikes_map: return None, None, None

        avail_strikes = sorted([float(s) for s in short_strikes_map.keys()])
        # 找 First OTM
        short_strike = next((s for s in avail_strikes if s > price * 1.01), None)
        if not short_strike: short_strike = avail_strikes[-1]

        # ---------------------------------------------------------
        # 2. 寻找 Long Leg (买方腿) - 自适应深度搜索
        # ---------------------------------------------------------
        long_candidates = []
        for exp_key in call_map.keys():
            parts = exp_key.split(":")
            exp_date = parts[0]
            try: days = int(parts[1])
            except: continue
            if days > 120: long_candidates.append((exp_date, days))
        
        if not long_candidates: return None, None, None

        # 优先选 150-450 天
        ideal = [c for c in long_candidates if 150 <= c[1] <= 450]
        if ideal:
            ideal.sort(key=lambda x: x[1])
            best_long_exp = ideal[0][0]
            best_long_days = ideal[0][1]
        else:
            long_candidates.sort(key=lambda x: x[1])
            best_long_exp = long_candidates[0][0]
            best_long_days = long_candidates[0][1]
        
        long_strikes_map = None
        # 模糊匹配 Long Exp Key
        for k in call_map.keys():
            if k.startswith(best_long_exp):
                long_strikes_map = call_map[k]
                break
        
        if not long_strikes_map: return None, None, None

        long_avail_strikes = sorted([float(s) for s in long_strikes_map.keys()])
        
        # [核心优化] 智能搜索最佳 Long Strike
        # 我们从 Price * 0.75 开始往下找，直到找到一个由 Width > Debit 构成的组合
        # 或者实在找不到，就返回最深那个
        
        # 先获取 Short Leg 的大致价格 (用于估算)
        # 注意：这里我们拿不到精确的 Short Mid，因为那是 Orchestrator 的事，但我们可以粗略估算
        # 为了简单，我们只做 Strike层面的几何检查：
        # 我们希望 Long Strike 足够低。
        
        # 策略：遍历所有小于 Price * 0.80 的 Strike，从高到低检查
        # 其实越低越安全，所以我们直接找 Price * 0.65 附近的试试？
        # 或者更稳妥：直接找最接近 Price * 0.70 的
        
        target_strike = price * 0.70 # 从 0.75 下调到 0.70，也就是买更深度的实值
        long_strike = min(long_avail_strikes, key=lambda x: abs(x - target_strike))

        # [进阶] 如果你想更激进地消除 Warning，可以取消下面的注释，强制选更小的
        # valid_deep_strikes = [s for s in long_avail_strikes if s <= price * 0.70]
        # if valid_deep_strikes:
        #     long_strike = valid_deep_strikes[-1] # 选符合条件里最大的那个 (也就是最接近0.70的)

        return short_strike, best_long_exp, long_strike

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        regime = str(tsf["regime"])
        hv_rank = float(ctx.hv.hv_rank)
        
        # 借用 scan row 字段作为 Short Leg 参照
        short_exp = str(tsf["short_exp"])
        short_dte = int(tsf["short_dte"])
        
        # 核心：寻找最佳的 PMCC 结构
        short_strike, long_exp, long_strike = self._find_strikes(ctx, short_exp)
        
        # --- Scoring (0-100) ---
        bd = ScoreBreakdown(base=50)

        # 1. HV Rank: PMCC 既买长 Vega 又卖短 Vega
        # 理想情况是低波动进场（买得便宜），或者适中波动
        # 如果波动率极高，买 LEAPS 会非常贵
        if hv_rank < 40:
            bd.hv = +10
        elif hv_rank > 70:
            bd.hv = -10 # 太贵了

        # 2. Regime: 必须是 Contango
        # 我们希望远期 IV 低（买便宜），近期 IV 高（卖得贵）
        if regime == "CONTANGO":
            bd.regime = +15
        elif regime == "BACKWARDATION":
            bd.regime = -20 # 倒挂是 PMCC 杀手

        # 3. Structure Availability
        if short_strike and long_strike:
            bd.edge = +10 # 找到了结构
        else:
            bd.penalties = -50 # 没找到结构

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties

        # --- Risk (0-100) ---
        rbd = RiskBreakdown(base=40)
        
        if regime == "BACKWARDATION":
            rbd.regime = +20 # 倒挂风险极高

        # 如果近期波动率过低，卖出的期权不值钱，风险增加（收益不足以覆盖 LEAPS 损耗）
        if float(tsf["short_iv"]) < 0.15:
            rbd.gamma = +10

        risk = rbd.base + rbd.dte + rbd.gamma + rbd.regime

        tag = "PMCC"
        if regime == "CONTANGO": tag += "-C"

        row = ScanRow(
            symbol=ctx.symbol,
            price=float(ctx.price),
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=float(tsf["short_iv"]),
            base_iv=float(tsf["base_iv"]),
            edge=0.0, # PMCC 不纯粹依赖 HV/IV Edge
            hv_rank=hv_rank,
            regime=regime,
            curvature=str(tsf["curvature"]),
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=rbd,
        )
        
        # 将我们精心计算的 Long Leg 信息挂载到 meta
        # Orchestrator 会读取这个 meta 来生成 DiagonalBlueprint
        row.meta = {
            "strategy": "diagonal",
            "short_strike": short_strike,
            "long_exp": long_exp,
            "long_strike": long_strike
        }
        
        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        # 暂不实现复杂的 Recommend 文本逻辑
        return None, "-"