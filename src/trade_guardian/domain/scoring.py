from __future__ import annotations

from dataclasses import dataclass

from trade_guardian.domain.models import ScoreBreakdown


@dataclass(frozen=True)
class ScoringRules:
    # core
    min_edge_short_base: float = 1.05

    # HV-aware (Strategy #2)
    hv_enabled: bool = False
    hv_low_rank: float = 20.0
    hv_mid_rank: float = 50.0
    hv_high_rank: float = 70.0

    hv_low_bonus: int = 10     # hv_rank <= low
    hv_mid_bonus: int = 4      # (low, mid]
    hv_high_penalty: int = -4  # (mid, high]
    hv_extreme_penalty: int = -10  # > high


class Scoring:
    def __init__(self, rules: ScoringRules):
        self.rules = rules

    def _hv_points(self, hv_rank: float) -> int:
        """
        HV scoring (explainable bucket model):
          - low hv_rank: calendars generally benefit from "room for vol expansion"
          - high hv_rank: you're paying rich vol; calendar can become "chasing vol"
        """
        if not self.rules.hv_enabled:
            return 0

        r = float(hv_rank)
        if r <= self.rules.hv_low_rank:
            return int(self.rules.hv_low_bonus)
        if r <= self.rules.hv_mid_rank:
            return int(self.rules.hv_mid_bonus)
        if r <= self.rules.hv_high_rank:
            return int(self.rules.hv_high_penalty)
        return int(self.rules.hv_extreme_penalty)

    def score_calendar(self, regime: str, curvature: str, edge: float, hv_rank: float) -> tuple[int, ScoreBreakdown]:
        """
        Keep simple & explainable:
          base 50
          +curv bonus when SPIKY_FRONT
          edge: reward if >= min_edge_short_base, penalty if weak
          regime: penalize CONTANGO a bit, neutral FLAT, small bonus BACKWARDATION
          hv: optional bucketed adjustment (Strategy #2)
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

        # hv (strategy #2)
        bd.hv = self._hv_points(hv_rank)

        total = bd.base + bd.regime + bd.edge + bd.hv + bd.curvature + bd.penalties
        return int(total), bd
