from __future__ import annotations

from typing import Dict, List

from trade_guardian.domain.models import HVInfo, TermPoint
from trade_guardian.domain.policy import ShortLegPolicy


class TSFeatureBuilder:
    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    @staticmethod
    def _eligible_points(term: List[TermPoint], min_dte: int) -> List[TermPoint]:
        return [p for p in term if p.dte >= min_dte]

    @staticmethod
    def _baseline_iv(term: List[TermPoint], fallback_iv: float) -> float:
        mids = [p.iv for p in term if 30 <= p.dte <= 90 and p.iv > 0]
        if mids:
            return float(sum(mids) / len(mids))
        return float(fallback_iv)

    def build(self, term: List[TermPoint], hv: HVInfo, rank: int) -> Dict[str, object]:
        if not term:
            return {"status": "Error", "msg": "Empty term structure"}

        eligible = self._eligible_points(term, self.policy.min_dte)
        if not eligible:
            return {"status": "Error", "msg": f"No eligible expiries (min_dte={self.policy.min_dte})"}

        if rank < 0 or rank >= len(eligible):
            return {"status": "Error", "msg": f"Rank out of range: rank={rank} eligible={len(eligible)}"}

        short = eligible[rank]
        base_iv = self._baseline_iv(term, fallback_iv=short.iv)

        # regime: compare base vs short
        if base_iv > short.iv * 1.03:
            regime = "CONTANGO"
        elif short.iv > base_iv * 1.03:
            regime = "BACKWARDATION"
        else:
            regime = "FLAT"

        # curvature: compare rank0 (nearest eligible) vs short
        front = eligible[0]
        squeeze_ratio = (front.iv / base_iv) if base_iv > 0 else 0.0

        # spiky front when rank0 materially richer than short rank
        curv = "SPIKY_FRONT" if (front.iv > short.iv * 1.20 and front.dte < 14) else "NORMAL"

        edge = (short.iv / base_iv) if base_iv > 0 else 0.0

        return {
            "status": "Success",
            "regime": regime,
            "curvature": curv,
            "short_exp": short.exp,
            "short_dte": short.dte,
            "short_iv": short.iv,
            "base_iv": base_iv,
            "edge": edge,
            "squeeze_ratio": squeeze_ratio,
        }
