from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Tuple, List, Dict, Any

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

        # --- NEW: PMCC delta-based strike selection (defaults are conservative) ---
        # short covered call: target ~0.30, stay OTM with small buffer
        self.short_call_delta_target = float(diag_cfg.get("short_call_delta_target", 0.30))
        self.short_call_delta_min = float(diag_cfg.get("short_call_delta_min", 0.15))
        self.short_call_delta_max = float(diag_cfg.get("short_call_delta_max", 0.40))
        self.short_call_otm_min_pct = float(diag_cfg.get("short_call_otm_min_pct", 0.01))  # 1% OTM buffer

        # long call (PMCC): usually ITM delta 0.65~0.85, target ~0.75
        self.long_call_delta_target = float(diag_cfg.get("long_call_delta_target", 0.75))
        self.long_call_delta_min = float(diag_cfg.get("long_call_delta_min", 0.60))
        self.long_call_delta_max = float(diag_cfg.get("long_call_delta_max", 0.90))

        # liquidity sanity
        self.min_bid = float(diag_cfg.get("min_bid", 0.05))
        self.min_oi = int(diag_cfg.get("min_open_interest", 0))

        # --- Long expiry controls (trade long leg selection) ---
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

        pool = [p for p in term if min_dte_eff <= int(p.dte) <= self.diag_long_max_dte]
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

    # ----------------------------
    # NEW: robust exp_key picker
    # ----------------------------
    @staticmethod
    def _find_exp_key(call_map: Dict[str, Any], exp_iso: str, dte_hint: Optional[int] = None) -> Optional[str]:
        if not call_map:
            return None
        keys = list(call_map.keys())

        # 1) exact startswith match
        for k in keys:
            if str(k).startswith(str(exp_iso)):
                return k

        # 2) dte hint match (handles cases where exp_iso mismatch but DTE aligns)
        if dte_hint is not None:
            best_k = None
            best_dist = 10**9
            for k in keys:
                try:
                    parts = str(k).split(":")
                    dte = int(parts[1]) if len(parts) > 1 else None
                    if dte is None:
                        continue
                    dist = abs(dte - int(dte_hint))
                    if dist < best_dist:
                        best_dist = dist
                        best_k = k
                except Exception:
                    continue
            if best_k is not None:
                return best_k

        # 3) fallback: first key (deterministic by sort)
        return sorted(keys)[0]

    # ----------------------------
    # NEW: strike selection by delta
    # ----------------------------
    def _pick_call_strike_by_delta(
        self,
        chain: dict,
        exp_iso: str,
        spot: float,
        *,
        dte_hint: Optional[int],
        want: str,  # "SHORT" or "LONG"
    ) -> Optional[float]:
        call_map = chain.get("callExpDateMap", {}) or {}
        exp_key = self._find_exp_key(call_map, exp_iso, dte_hint=dte_hint)
        if not exp_key:
            return None

        strikes_map = call_map.get(exp_key, {}) or {}
        if not isinstance(strikes_map, dict) or not strikes_map:
            return None

        candidates: List[Tuple[float, float, float]] = []
        # tuple: (score, strike, delta)

        if want == "SHORT":
            tgt = float(self.short_call_delta_target)
            dmin = float(self.short_call_delta_min)
            dmax = float(self.short_call_delta_max)
            min_strike = float(spot) * (1.0 + float(self.short_call_otm_min_pct))

            for s_str, q_list in strikes_map.items():
                try:
                    strike = float(s_str)
                except Exception:
                    continue
                if strike < min_strike:
                    continue
                if not q_list:
                    continue
                q = q_list[0] or {}
                bid = float(q.get("bid") or 0.0)
                ask = float(q.get("ask") or 0.0)
                oi = int(q.get("openInterest") or 0)

                if bid < self.min_bid or ask <= 0:
                    continue
                if self.min_oi > 0 and oi < self.min_oi:
                    continue

                delta = float(q.get("delta") or 0.0)
                if delta <= 0:
                    continue

                # delta band
                if not (dmin <= delta <= dmax):
                    continue

                # score: primarily delta closeness; add small penalty for being too close to spot
                delta_score = abs(delta - tgt)
                mny_penalty = abs(strike - spot) / max(1.0, spot) * 0.10
                score = delta_score + mny_penalty
                candidates.append((score, strike, delta))

        else:  # want == "LONG"
            tgt = float(self.long_call_delta_target)
            dmin = float(self.long_call_delta_min)
            dmax = float(self.long_call_delta_max)

            for s_str, q_list in strikes_map.items():
                try:
                    strike = float(s_str)
                except Exception:
                    continue
                # ITM / near-ITM: strike <= spot
                if strike > spot:
                    continue
                if not q_list:
                    continue
                q = q_list[0] or {}
                bid = float(q.get("bid") or 0.0)
                ask = float(q.get("ask") or 0.0)
                oi = int(q.get("openInterest") or 0)

                if bid < self.min_bid or ask <= 0:
                    continue
                if self.min_oi > 0 and oi < self.min_oi:
                    continue

                delta = float(q.get("delta") or 0.0)
                if delta <= 0:
                    continue

                if not (dmin <= delta <= dmax):
                    continue

                # score: delta closeness; mild penalty for being too deep ITM (keeps cost reasonable)
                delta_score = abs(delta - tgt)
                depth_penalty = abs(spot - strike) / max(1.0, spot) * 0.05
                score = delta_score + depth_penalty
                candidates.append((score, strike, delta))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return float(candidates[0][1])

        # fallback (no candidates): keep old simple behavior but safer
        strike_list: List[float] = []
        for s_str in strikes_map.keys():
            try:
                strike_list.append(float(s_str))
            except Exception:
                continue
        if not strike_list:
            return None
        strike_list.sort()

        if want == "SHORT":
            # fallback: choose further OTM than before (>= spot*(1+otm_min_pct))
            min_strike = float(spot) * (1.0 + float(self.short_call_otm_min_pct))
            for s in strike_list:
                if s >= min_strike:
                    return float(s)
            return float(strike_list[-1])

        # LONG fallback: closest ITM strike
        itms = [s for s in strike_list if s <= spot]
        return float(itms[-1]) if itms else float(strike_list[0])

    def _find_strikes_with_exp(
        self, ctx: Context, short_exp: str, long_exp: str, short_dte: int, long_dte_hint: Optional[int]
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        spot = float(ctx.price)
        chain = ctx.raw_chain

        if not short_exp or not long_exp:
            return None, None, None

        # pick short strike by delta (~0.30)
        short_strike = self._pick_call_strike_by_delta(
            chain, short_exp, spot,
            dte_hint=int(short_dte) if short_dte else None,
            want="SHORT",
        )
        if short_strike is None:
            return None, None, None

        # pick long strike by delta (~0.75 ITM)
        long_strike = self._pick_call_strike_by_delta(
            chain, long_exp, spot,
            dte_hint=int(long_dte_hint) if long_dte_hint else None,
            want="LONG",
        )
        if long_strike is None:
            return None, None, None

        # ensure long strike < short strike (standard call diagonal / PMCC)
        if long_strike >= short_strike:
            # force long lower than short
            long_strike = min(long_strike, short_strike - 0.5)

        return float(short_strike), str(long_exp), float(long_strike)

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

        # Anchor (structure)
        anchor_exp = str(tsf.get("month_exp"))
        anchor_dte = int(tsf.get("month_dte", 0))
        anchor_iv = float(tsf.get("month_iv", 0.0))
        anchor_edge_month = float(tsf.get("edge_month", 0.0))

        # Scheme A: pick TRADE long expiry from ctx.term
        long_exp_trade = self._pick_diag_long_exp(ctx, short_dte=short_dte)
        if not long_exp_trade:
            long_exp_trade = anchor_exp

        # get long_dte hint for exp_key matching
        tp_long = self._term_point_by_exp(ctx, str(long_exp_trade)) if long_exp_trade else None
        long_dte_hint = int(tp_long.dte) if tp_long and tp_long.dte is not None else None

        # strikes (delta-based)
        short_strike, long_exp, long_strike = self._find_strikes_with_exp(
            ctx, short_exp, str(long_exp_trade), short_dte=short_dte, long_dte_hint=long_dte_hint
        )

        # month_* represents TRADE long leg (same as blueprint)
        IV_FLOOR = 12.0
        month_exp = str(long_exp_trade or "")
        month_dte = 0
        month_iv = 0.0

        tp = self._term_point_by_exp(ctx, month_exp) if month_exp else None
        if tp:
            month_dte = int(tp.dte or 0)
            month_iv = _iv_to_pct(tp.iv)

        if month_iv <= 0:
            month_iv = _iv_to_pct(anchor_iv)

        denom = max(IV_FLOOR, float(short_iv or 0.0) if float(short_iv or 0.0) > 0 else IV_FLOOR)
        edge_month_trade = (float(month_iv) - float(short_iv or 0.0)) / denom

        # Scoring
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

                "month_exp": month_exp,
                "month_dte": month_dte,
                "month_iv": float(month_iv),
                "edge_month": float(edge_month_trade),

                "anchor_exp": anchor_exp,
                "anchor_dte": anchor_dte,
                "anchor_iv": float(_iv_to_pct(anchor_iv)),
                "anchor_edge_month": float(anchor_edge_month),

                "diag_long_exp_src": "term_pick" if long_exp_trade and long_exp_trade != anchor_exp else "anchor_fallback",

                # NEW: expose delta targets (for debugging)
                "short_call_delta_target": self.short_call_delta_target,
                "long_call_delta_target": self.long_call_delta_target,
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
