from __future__ import annotations
from typing import Tuple, List
from trade_guardian.domain.models import Context, ScanRow, ScoreBreakdown, RiskBreakdown, Blueprint
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy

ETF_LIST = ["SPY", "QQQ", "IWM", "TQQQ", "SQQQ", "SOXL", "SOXS", "TSLL", "TSLS", "NVDL", "UVXY", "TLT"]

class LongGammaStrategy(Strategy):
    name = "long_gamma"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        self.rules = cfg.get("rules", {})
        
        self.min_dte_etf = self.rules.get("lg_min_dte_etf", 7)
        self.min_dte_stock = self.rules.get("lg_min_dte_stock", 10)
        self.pin_cap = self.rules.get("pin_risk_cap", 0.25)
        self.pin_coeff = self.rules.get("pin_risk_coeff", 0.3)
        self.max_spread = self.rules.get("lg_max_spread_pct", 0.08)

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _strikes_from_chain(self, ctx: Context, exp: str, side: str="CALL") -> List[float]:
        """
        [FIX] 直接从 raw_chain 获取该到期日的所有 Strike，用于算真实 Step
        """
        key = "callExpDateMap" if side=="CALL" else "putExpDateMap"
        m = ctx.raw_chain.get(key, {}) or {}
        
        target_key = None
        for k in m.keys():
            if k.startswith(exp):
                target_key = k
                break
        
        if target_key and target_key in m:
            return sorted([float(s) for s in m[target_key].keys()])
        return []

    def _get_real_strike_step(self, ctx: Context, exp: str) -> float:
        strikes = self._strikes_from_chain(ctx, exp)
        if len(strikes) < 2: return 1.0

        # 只看 ATM 附近 10 个 strike
        center_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - ctx.price))
        start = max(0, center_idx - 5)
        end = min(len(strikes), center_idx + 5)
        subset = strikes[start:end]
        
        diffs = [subset[i+1] - subset[i] for i in range(len(subset)-1)]
        if not diffs: return 1.0
        
        # 取最小正差值
        return min(d for d in diffs if d > 0)

    def _check_pin_risk(self, ctx: Context, price: float, dte: int, exp: str) -> Tuple[bool, str]:
        if dte > 3: return False, ""
        
        real_step = self._get_real_strike_step(ctx, exp)
        threshold = min(self.pin_cap, self.pin_coeff * real_step)
        
        nearest_anchor = round(price / real_step) * real_step
        dist = abs(price - nearest_anchor)
        
        if dist < threshold:
            return True, f"Pinned: Dist {dist:.2f} < {threshold:.2f} (Step {real_step})"
            
        return False, ""

    def _empty_row(self, ctx, note):
        bd = ScoreBreakdown(base=0)
        return ScanRow(
            symbol=ctx.symbol, price=float(ctx.price),
            short_exp="N/A", short_dte=0, short_iv=0, base_iv=0, edge=0, hv_rank=0,
            regime="N/A", curvature="N/A", tag="LG-FAIL",
            cal_score=0, short_risk=99,
            score_breakdown=bd, risk_breakdown=RiskBreakdown(),
            meta={"error": note},
            blueprint=Blueprint(ctx.symbol, "LG-FAIL", [], 0.0, error=note, note=note)
        )

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        symbol = ctx.symbol
        price = float(ctx.price)
        
        short_iv = float(tsf.get("short_iv", 0.0))
        edge_micro = float(tsf.get("edge_micro", 0.0))
        edge_month = float(tsf.get("edge_month", 0.0))
        short_dte = int(tsf.get("short_dte", 0))
        short_exp = str(tsf.get("short_exp", ""))
        
        # 1. DTE Hard Kill
        is_etf = symbol in ETF_LIST
        min_dte = self.min_dte_etf if is_etf else self.min_dte_stock
        has_catalyst = False 
        
        if short_dte < min_dte and not has_catalyst:
            return self._empty_row(ctx, f"DTE {short_dte} < {min_dte} (Theta Risk)")

        # 2. Pin Risk Hard Kill (Data-Driven)
        is_pinned, pin_msg = self._check_pin_risk(ctx, price, short_dte, short_exp)
        if is_pinned:
            return self._empty_row(ctx, pin_msg)

        # 3. 评分逻辑
        score = 60
        score += int(self._clamp(edge_micro * 40, -20, 20))
        score += int(self._clamp(edge_month * 60, -20, 30))
        score = self._clamp(score, 0, 100)

        # 4. 构造 Gate
        gate = "WAIT"
        if score > 75 and edge_micro > 0.05 and edge_month > 0.10:
            gate = "READY"
        
        if symbol in ["TSLL", "TQQQ", "SOXL", "ONDS", "SMCI"]:
             gate = "FORBID"

        tag = "LG"
        if edge_micro > 0.15: tag += "-M"
        if edge_month > 0.30: tag += "-K"

        bd = ScoreBreakdown(base=60) 
        rbd = RiskBreakdown(base=20)
        
        row = ScanRow(
            symbol=symbol,
            price=price,
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=short_iv,
            base_iv=tsf.get("month_iv", 0.0),
            edge=edge_month,
            hv_rank=50.0,
            regime="NORMAL",
            curvature="FLAT",
            tag=tag,
            cal_score=int(score),
            short_risk=20,
            score_breakdown=bd,
            risk_breakdown=rbd,
        )
        
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
            "strike": round(price, 1),
            "max_spread_pct": self.max_spread, # 传递给 Orchestrator
            "stop_loss_rules": {
                "type": "time_based",
                "minutes": self.rules.get("lg_time_stop_min", 90),
                "no_move_pct": self.rules.get("lg_no_move_frac", 0.25)
            }
        }
        return row