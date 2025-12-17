from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import (
    Context,
    Recommendation,
    ScanRow,
    ScoreBreakdown,
    RiskBreakdown,
)
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.domain.scoring import Scoring, ScoringRules


class CalendarStrategy:
    name = "calendar"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        rules = ScoringRules(min_edge_short_base=float(cfg["rules"]["min_edge_short_base"]))
        self.scoring = Scoring(rules)

    def _tag(self, regime: str, curvature: str) -> str:
        t = "F" if regime == "FLAT" else ("C" if regime == "CONTANGO" else "B")
        if curvature == "SPIKY_FRONT":
            t += "S"
        return t

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _risk_score(
        self,
        ctx: Context,
        *,
        short_dte: int,
        short_gamma: float,
        rank0_dte: int,
        rank0_gamma: float,
        regime: str,
        curvature: str,
        squeeze_ratio: float,
    ) -> tuple[int, RiskBreakdown]:
        """
        Continuous & explainable risk model (0..100):

        base: start at 35 (not 50) so the system has headroom.
        dte: continuous decay with DTE (shorter => higher risk)
        gamma: continuous penalty based on gamma normalized vs front (or max)
        curvature: continuous penalty based on squeeze_ratio (only when SPIKY_FRONT)
        regime: small penalty (BACKWARDATION > CONTANGO > FLAT)

        We also keep breakdown fields as integers for stable CLI output.
        """
        bd = RiskBreakdown(base=35)

        # -------- DTE penalty (continuous) --------
        # Target behavior (approx):
        #   dte ~ 1-3  => big penalty
        #   dte ~ 6    => medium
        #   dte ~ 10   => smaller
        #   dte >= 21  => near 0
        d = float(max(0, short_dte))
        # smooth curve: 0..~22 then clamp
        dte_pen = 26.0 / (1.0 + (d / 6.5) ** 1.25)  # d=6 => ~14-16 ; d=10 => ~9-11 ; d=20 => ~4
        # extra caution if front expiry is extremely close (weekly Friday effect / 0DTE clusters)
        front_pen = 0.0
        if rank0_dte <= 1:
            front_pen = 6.0
        elif rank0_dte <= 3:
            front_pen = 3.0

        bd.dte = int(round(self._clamp(dte_pen + front_pen, 0.0, 30.0)))

        # -------- Gamma penalty (continuous) --------
        # Normalize gamma: if we have rank0_gamma use it; otherwise fall back to max gamma in eligible term
        denom = rank0_gamma if rank0_gamma and rank0_gamma > 0 else 0.0
        if denom <= 0:
            # fallback: max gamma among term points we have
            try:
                denom = max(float(p.gamma) for p in ctx.term if p.gamma is not None)  # type: ignore
            except Exception:
                denom = 0.0

        g = float(short_gamma) if short_gamma is not None else 0.0
        g_norm = (g / denom) if denom > 0 else 0.0
        g_norm = self._clamp(g_norm, 0.0, 2.0)  # allow >1 if short gamma > front (rare but possible)

        # penalty curve: small when g_norm <=0.3, grows faster after 0.6
        # map roughly into 0..22
        gamma_pen = 22.0 * (g_norm ** 0.75)
        bd.gamma = int(round(self._clamp(gamma_pen, 0.0, 22.0)))

        # -------- Curvature penalty (continuous) --------
        # Use squeeze_ratio: (rank0_iv / base_iv) as a "front spike" severity.
        # Only penalize meaningfully if SPIKY_FRONT and squeeze is above mild threshold.
        curv_pen = 0.0
        if curvature == "SPIKY_FRONT":
            sr = float(squeeze_ratio) if squeeze_ratio is not None else 0.0
            # thresholded linear ramp:
            #  sr <= 1.10 => ~0
            #  sr 1.10..1.80 => 0..10
            curv_pen = 10.0 * self._clamp((sr - 1.10) / 0.70, 0.0, 1.0)

        bd.curv = int(round(self._clamp(curv_pen, 0.0, 10.0)))

        # -------- Regime penalty (small, not dominating) --------
        # BACKWARDATION means front richer -> short leg can be more dangerous (bigger adverse gamma & gap risk).
        if regime == "BACKWARDATION":
            bd.regime = 4
        elif regime == "CONTANGO":
            bd.regime = 2
        else:
            bd.regime = 0

        # -------- Penalties slot (reserved) --------
        bd.penalties = 0

        total = bd.base + bd.dte + bd.gamma + bd.curv + bd.regime + bd.penalties
        return int(self._clamp(float(total), 0.0, 100.0)), bd

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        regime = str(tsf["regime"])
        curvature = str(tsf["curvature"])
        short_exp = str(tsf["short_exp"])
        short_dte = int(tsf["short_dte"])
        short_iv = float(tsf["short_iv"])
        base_iv = float(tsf["base_iv"])
        edge = float(tsf["edge"])
        squeeze_ratio = float(tsf.get("squeeze_ratio", 0.0))

        eligible = [p for p in ctx.term if p.dte >= self.policy.min_dte]
        if not eligible:
            # fallback: no eligible list, treat chosen short as the only reference
            rank0_dte = short_dte
            rank0_gamma = 0.0
            short_gamma = 0.0
        else:
            # rank0 refers to nearest *eligible* expiry (respect MIN_SHORT_DTE policy)
            rank0 = eligible[0]
            rank0_dte = int(rank0.dte)
            rank0_gamma = float(rank0.gamma) if rank0.gamma is not None else 0.0

            # find the chosen short point in eligible chain by matching exp or dte
            short_point = None
            for p in eligible:
                if str(p.exp) == short_exp:
                    short_point = p
                    break
            if short_point is None:
                # fallback by dte match
                for p in eligible:
                    if int(p.dte) == short_dte:
                        short_point = p
                        break
            short_gamma = float(short_point.gamma) if (short_point and short_point.gamma is not None) else 0.0

        score, bd = self.scoring.score_calendar(
            regime=regime,
            curvature=curvature,
            edge=edge,
            hv_rank=ctx.hv.hv_rank,
        )

        risk, rbd = self._risk_score(
            ctx,
            short_dte=short_dte,
            short_gamma=short_gamma,
            rank0_dte=rank0_dte,
            rank0_gamma=rank0_gamma,
            regime=regime,
            curvature=curvature,
            squeeze_ratio=squeeze_ratio,
        )

        tag = self._tag(regime, curvature)

        return ScanRow(
            symbol=ctx.symbol,
            price=float(ctx.price),
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=short_iv,
            base_iv=base_iv,
            edge=edge,
            hv_rank=float(ctx.hv.hv_rank),
            regime=regime,
            curvature=curvature,
            tag=tag,
            cal_score=int(score),
            short_risk=int(risk),
            score_breakdown=bd,
            risk_breakdown=rbd,
        )

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        """
        Probe ranks base..base+N-1 and return first rank with risk<=max_risk AND score>=min_score.
        If none, return (None, summary).
        """
        ranks = self.policy.probe_ranks()
        eligible = [p for p in ctx.term if p.dte >= self.policy.min_dte]
        if not eligible:
            return None, "-"

        # constants for risk computation
        regime = str(ctx.tsf["regime"])
        curvature = str(ctx.tsf["curvature"])
        squeeze_ratio = float(ctx.tsf.get("squeeze_ratio", 0.0))

        rank0 = eligible[0]
        rank0_dte = int(rank0.dte)
        rank0_gamma = float(rank0.gamma) if rank0.gamma is not None else 0.0

        best_attempt: Optional[Recommendation] = None
        best_summary = "-"

        for rk in ranks:
            if rk < 0 or rk >= len(eligible):
                continue

            p = eligible[rk]
            base_iv = float(ctx.tsf["base_iv"])
            edge = (float(p.iv) / base_iv) if base_iv > 0 else 0.0

            score, bd = self.scoring.score_calendar(
                regime=regime,
                curvature=curvature,
                edge=edge,
                hv_rank=ctx.hv.hv_rank,
            )

            short_gamma = float(p.gamma) if p.gamma is not None else 0.0
            risk, _ = self._risk_score(
                ctx,
                short_dte=int(p.dte),
                short_gamma=short_gamma,
                rank0_dte=rank0_dte,
                rank0_gamma=rank0_gamma,
                regime=regime,
                curvature=curvature,
                squeeze_ratio=squeeze_ratio,
            )

            tag = self._tag(regime, curvature)

            # first tradable that satisfies thresholds
            if score >= min_score and risk <= max_risk:
                rec = Recommendation(
                    rec_rank=int(rk),
                    rec_exp=str(p.exp),
                    rec_dte=int(p.dte),
                    rec_iv=float(p.iv),
                    rec_edge=float(edge),
                    rec_score=int(score),
                    rec_risk=int(risk),
                    rec_tag=tag,
                    rec_breakdown=bd,
                )
                summary = f"ok rk{rk} {p.exp} d{p.dte} e{edge:.2f} s{score} r{risk} {tag}"
                return rec, summary

            # track best attempt summary for watchlist
            if best_attempt is None or score > best_attempt.rec_score:
                best_attempt = Recommendation(
                    rec_rank=int(rk),
                    rec_exp=str(p.exp),
                    rec_dte=int(p.dte),
                    rec_iv=float(p.iv),
                    rec_edge=float(edge),
                    rec_score=int(score),
                    rec_risk=int(risk),
                    rec_tag=tag,
                    rec_breakdown=bd,
                )
                best_summary = f"best rk{rk} {p.exp} d{p.dte} e{edge:.2f} s{score} r{risk} {tag}"

        return None, best_summary
