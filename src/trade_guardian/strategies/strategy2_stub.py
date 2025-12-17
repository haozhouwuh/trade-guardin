from typing import Optional, Tuple

from trade_guardian.domain.models import Context, ScanRow, Recommendation, ScoreBreakdown


class Strategy2Stub:
    """
    Reserved for strategy #2. Framework is in place.
    This stub intentionally does not implement real logic yet.
    """
    name = "strategy2"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def evaluate(self, ctx: Context) -> ScanRow:
        raise NotImplementedError("strategy2 is not implemented yet.")

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        return None, "-"
