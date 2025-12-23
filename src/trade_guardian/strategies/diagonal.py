from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Tuple, List

from trade_guardian.domain.models import (
    Context, Recommendation, ScanRow, ScoreBreakdown, RiskBreakdown, Blueprint, TermPoint
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy
from trade_guardian.strategies.blueprint import build_diagonal_blueprint


def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()


def _is_third_friday(d: date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


def _iv_to_pct(iv: float) -> float:
    """Schwab iv may be 0.xx or already percent; keep consistent with build_context heuristic."""
    v = float(iv or 0.0)
    if 0 < v < 1.5:
        v *= 100.0
    return v


class DiagonalStrategy(Strategy):
    name = "diagonal"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

        # --- Short leg controls (existing behavior) ---
        diag_cfg = cfg.get("strategies", {}).get("diagonal", {})
        self.short_min_dte = int(diag_cfg.get("short_min_dte", 1))
        self.short_max_dte = int(diag_cfg.get("short_max_dte", 14))
        self.use_rank_0 = bool(diag_cfg.get("use_rank_0_for_short", False))

        # --- Long leg controls (trade long leg selection) ---
        rules = cfg.get("rules", {})
        self.diag_long_min_dte = int(rules.get("diag_long_min_dte", 30))
        self.diag_long_max_dte = int(rules.get("diag_long_max_dte", 45))
        self.diag_long_fallback_max_dte = int(rules.get("diag_long_fallback_max_dte", 90))
        self.diag_long_target_dte = float(rules.get("diag_long_target_dte", 38))
        self.diag_long_lambda_dist = float(rules.get("diag_long_lambda_dist", 0.25))
        self.diag_long_prefer_monthly = bool(rules.get("diag_long_prefer_monthly", False))
        self.diag_long_min_gap_vs_short = int(rules.get("diag_long_min_gap_vs_short", 20))

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _term_point_by_exp(self, ctx: Context, exp: str) -> Optional[TermPoint]:
        term: List[TermPoint] = getattr(ctx, "term", []) or []
        for p in term:
            if str(p.exp) == str(exp):
                return p
        return None

    def _pick_diag_long_exp(self, ctx: Context, short_dte: int) -> Optional[str]:
        """
        Trade long leg expiry selector (scheme A).
        Chooses from ctx.term using rules.diag_long_*.
        """
        term: List[TermPoint] = getattr(ctx, "term", []) or []
        if not term:
            return None

        min_dte_eff = max(self.diag_long_min_dte, int(short_dte) + self.diag_long_min_gap_vs_short)

        # main window
        pool = [p for p in term if min_dte_eff <= int(p.dte) <= self.diag_long_max_dte]
        # fallback window
        if not pool:
            pool = [p for p in term if min_dte_eff <= int(p.dte) <= self.diag_long_fallback_max_dte]
        if not pool:
            far = [p for p in term if int(p.dte) >= min_dte_eff]
            if far:
                return max(far, key=lambda x: int(x.dte)).exp
            return None

        if self.diag_long_prefer_monthly:
            monthly = []
            for p in pool:
                try:
                    if _is_third_friday(_to_date(p.exp)):
                        monthly.append(p)
                except Exception:
                    continue
            if monthly:
                pool = monthly

        def score(p: TermPoint) -> float:
            dist = abs(float(p.dte) - self.diag_long_target_dte) / max(1.0, self.diag_long_target_dte)
            return self.diag_long_lambda_dist * dist

        best = min(pool, key=score)
        return best.exp

    def _find_strikes_with_exp(
        self, ctx: Context, short_exp: str, long_exp: str
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        price = float(ctx.price)
        chain = ctx.raw_chain

        if not short_exp or not long_exp:
            return None, None, None

        call_map = chain.get("callExpDateMap", {})

        def get_strikes_for_exp(exp_date_str: str) -> List[float]:
            for k, v in call_map.items():
                if k.startswith(exp_date_str):
                    return sorted([float(s) for s in v.keys()])
            return []

        short_strikes = get_strikes_for_exp(short_exp)
        long_strikes = get_strikes_for_exp(long_exp)

        if not short_strikes or not long_strikes:
            return None, None, None

        short_strike = next((s for s in short_strikes if s > price * 1.005), None)
        if short_strike is None:
            return None, None, None

        long_strike_candidates = [s for s in long_strikes if s <= price]
        long_strike = long_strike_candidates[-1] if long_strike_candidates else long_strikes[0]

        if long_strike >= short_strike:
            lower = [s for s in long_strikes if s < short_strike]
            long_strike = lower[-1] if lower else short_strike

        return short_strike, long_exp, long_strike

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = (ctx.tsf or {}).copy()

        hv_rank = float(ctx.hv.hv_rank)
        price = float(ctx.price)

        short_exp = str(tsf.get("short_exp"))
        short_dte = int(tsf.get("short_dte", 0))
        short_iv = float(tsf.get("short_iv", 0.0))

        # rank-0 override (existing behavior)
        nearest_dte = int(tsf.get("nearest_dte", 999))
        if self.use_rank_0 and nearest_dte < short_dte and nearest_dte >= self.short_min_dte:
            short_exp = str(tsf.get("nearest_exp"))
            short_dte = nearest_dte
            short_iv = float(tsf.get("nearest_iv", 0.0))

            tsf["short_exp"] = short_exp
            tsf["short_dte"] = short_dte
            tsf["short_iv"] = short_iv

        # --------------------------
        # Anchor (structure) still kept, but NOT used as month_* under scheme A
        # --------------------------
        anchor_exp = str(tsf.get("month_exp"))
        anchor_dte = int(tsf.get("month_dte", 0))
        anchor_iv = float(tsf.get("month_iv", 0.0))
        anchor_edge_month = float(tsf.get("edge_month", 0.0))

        # --------------------------
        # Scheme A: pick TRADE long expiry from ctx.term
        # --------------------------
        long_exp_trade = self._pick_diag_long_exp(ctx, short_dte=short_dte)
        if not long_exp_trade:
            # deterministic fallback: anchor exp
            long_exp_trade = anchor_exp

        # strikes
        short_strike, long_exp, long_strike = self._find_strikes_with_exp(ctx, short_exp, long_exp_trade)

        # --------------------------
        # Scheme A: month_* must represent TRADE long leg (same as blueprint)
        # --------------------------
        IV_FLOOR = 12.0
        month_exp = str(long_exp_trade or "")
        month_dte = 0
        month_iv = 0.0

        tp = self._term_point_by_exp(ctx, month_exp) if month_exp else None
        if tp:
            month_dte = int(tp.dte or 0)
            month_iv = _iv_to_pct(tp.iv)

        # if term missing, try tsf month_iv as last resort to avoid division by 0
        if month_iv <= 0:
            month_iv = _iv_to_pct(anchor_iv)

        denom = max(IV_FLOOR, float(short_iv or 0.0) if float(short_iv or 0.0) > 0 else IV_FLOOR)
        edge_month_trade = (float(month_iv) - float(short_iv or 0.0)) / denom

        # --- Scoring: use trade edge (so the row score reflects what you're actually trading) ---
        bd = ScoreBreakdown(base=50)
        bd.edge = int(self._clamp(edge_month_trade * 80, -20, 40))

        if short_strike and long_strike and long_exp:
            bd.base += 10
        else:
            bd.penalties = -999

        if edge_month_trade < 0:
            bd.regime = -30

        score = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        risk = 30
        if edge_month_trade < 0:
            risk += 40

        tag = "DIAG"
        if edge_month_trade > 0.15:
            tag += "+"

        row = ScanRow(
            symbol=ctx.symbol,
            price=price,
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=float(short_iv),
            base_iv=float(month_iv),
            edge=float(edge_month_trade),
            hv_rank=hv_rank,
            regime=str(tsf.get("regime")),
            curvature=str(tsf.get("curvature")),
            tag=tag,
            cal_score=int(self._clamp(float(score), 0, 100)),
            short_risk=int(self._clamp(float(risk), 0, 100)),
            score_breakdown=bd,
            risk_breakdown=RiskBreakdown(base=risk),
        )

        if short_strike and long_strike and long_exp:
            bp = build_diagonal_blueprint(
                symbol=ctx.symbol,
                underlying=price,
                chain=ctx.raw_chain,
                short_exp=short_exp,
                long_exp=long_exp,  # == month_exp under scheme A
                target_short_strike=short_strike,
                target_long_strike=long_strike,
                side="CALL",
            )
            row.blueprint = bp

            # ✅ Scheme A meta: month_* == blueprint long leg
            row.meta = {
                "strategy": "diagonal",
                "short_strike": short_strike,
                "long_exp": long_exp,
                "long_strike": long_strike,
                "est_gamma": float(ctx.metrics.gamma) if ctx.metrics else 0.0,

                "micro_exp": tsf.get("micro_exp"),
                "micro_dte": tsf.get("micro_dte"),
                "micro_iv": tsf.get("micro_iv"),
                "edge_micro": float(tsf.get("edge_micro", 0.0)),

                # ✅ month_* now represents TRADE long leg
                "month_exp": month_exp,
                "month_dte": month_dte,
                "month_iv": float(month_iv),
                "edge_month": float(edge_month_trade),

                # Keep anchor for visibility/debug (no longer used by table if you print month_*)
                "anchor_exp": anchor_exp,
                "anchor_dte": anchor_dte,
                "anchor_iv": float(_iv_to_pct(anchor_iv)),
                "anchor_edge_month": float(anchor_edge_month),

                "diag_long_exp_src": "term_pick" if long_exp_trade and long_exp_trade != anchor_exp else "anchor_fallback",
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
            meta=row.meta,
        )
        return rec, "OK"
