from __future__ import annotations

from dataclasses import dataclass

from trade_guardian.domain.models import ScoreBreakdown, RiskBreakdown


@dataclass(frozen=True)
class ScoringRules:
    # score rule
    min_edge_short_base: float = 1.05

    # risk knobs (keep them here; later you can wire to config.json)
    risk_base: int = 50

    # DTE risk thresholds
    risk_dte_le_3: int = 40
    risk_dte_le_7: int = 20
    risk_dte_le_14: int = 10

    # gamma risk (ATM-ish gamma, typical range depends on underlying)
    # We keep it coarse + explainable; you can refine later with percentiles per symbol.
    gamma_hi: float = 0.020
    gamma_mid: float = 0.010
    risk_gamma_hi: int = 20
    risk_gamma_mid: int = 10

    # curvature/regime risk
    risk_curv_spiky_front: int = 10
    risk_regime_backwardation: int = 10  # short>base often means paying up front-end
    risk_regime_contango: int = 5        # mild caution


class Scoring:
    def __init__(self, rules: ScoringRules):
        self.rules = rules

    def score_calendar(self, regime: str, curvature: str, edge: float, hv_rank: float) -> tuple[int, ScoreBreakdown]:
        """
        Keep simple & explainable:
          base 50
          +curv bonus when SPIKY_FRONT
          edge: reward if >= min_edge_short_base, penalty if weak
          regime: penalize CONTANGO a bit, neutral FLAT, small bonus BACKWARDATION
          hv: reserved (0 now) – future: low hv rank helps long gamma
        """
        bd = ScoreBreakdown(base=50)

        # regime
        if regime == "CONTANGO":
            bd.regime = -8
        elif regime == "BACKWARDATION":
            bd.regime = +4
        else:
            bd.regime = 0

        # curvature
        bd.curvature = +6 if curvature == "SPIKY_FRONT" else 0

        # edge
        if edge >= self.rules.min_edge_short_base:
            bd.edge = +8
        elif edge >= 1.0:
            bd.edge = -8
        else:
            bd.edge = -14

        # hv reserved (0 for now, keep slot for strategy#2/#3)
        bd.hv = 0

        total = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        return int(total), bd

    def risk_calendar(
        self,
        short_dte: int,
        short_gamma: float,
        regime: str,
        curvature: str,
    ) -> tuple[int, RiskBreakdown]:
        """
        Explainable risk model (0..100):
          base
          + DTE bucket risk (very short is dangerous)
          + gamma bucket risk (high gamma => short leg is twitchy)
          + curvature/regime small adjustments
          clamp to [0, 100]
        """
        r = RiskBreakdown()

        # base
        r.base = int(self.rules.risk_base)

        # DTE
        if short_dte <= 3:
            r.dte = int(self.rules.risk_dte_le_3)
        elif short_dte <= 7:
            r.dte = int(self.rules.risk_dte_le_7)
        elif short_dte <= 14:
            r.dte = int(self.rules.risk_dte_le_14)
        else:
            r.dte = 0

        # gamma buckets
        g = float(short_gamma or 0.0)
        if g >= self.rules.gamma_hi:
            r.gamma = int(self.rules.risk_gamma_hi)
        elif g >= self.rules.gamma_mid:
            r.gamma = int(self.rules.risk_gamma_mid)
        else:
            r.gamma = 0

        # curvature
        if curvature == "SPIKY_FRONT":
            r.curvature = int(self.rules.risk_curv_spiky_front)

        # regime (small nudges; keep explainable)
        if regime == "BACKWARDATION":
            r.regime = int(self.rules.risk_regime_backwardation)
        elif regime == "CONTANGO":
            r.regime = int(self.rules.risk_regime_contango)

        total = r.base + r.dte + r.gamma + r.curvature + r.regime + r.penalties

        # ✅ enforce invariant
        total = max(0, min(100, int(total)))
        return total, r
