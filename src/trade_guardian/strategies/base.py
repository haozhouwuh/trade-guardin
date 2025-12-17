from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate(self, ctx: Context) -> ScanRow:
        raise NotImplementedError

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        """
        Optional: probe ranks to find a tradable short leg.
        For strategies that don't support probing, return (None, "-").
        """
        return None, "-"
