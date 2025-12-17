from __future__ import annotations

from trade_guardian.domain.models import Context, ScanRow, ScoreBreakdown
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy


class PlaceholderStrategy(Strategy):
    """
    Strategy #2/#3 placeholder.
    Exists to prove the framework: registry, CLI, orchestrator wiring.
    Not intended for trading logic.
    """
    name = "placeholder"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    def evaluate(self, ctx: Context) -> ScanRow:
        bd = ScoreBreakdown(base=0)
        return ScanRow(
            symbol=ctx.symbol,
            price=float(ctx.price),
            short_exp=ctx.tsf.get("short_exp", ""),
            short_dte=int(ctx.tsf.get("short_dte", 0)),
            short_iv=float(ctx.tsf.get("short_iv", 0.0)),
            base_iv=float(ctx.tsf.get("base_iv", 0.0)),
            edge=float(ctx.tsf.get("edge", 0.0)),
            hv_rank=float(ctx.hv.hv_rank),
            regime=str(ctx.tsf.get("regime", "FLAT")),
            curvature=str(ctx.tsf.get("curvature", "NORMAL")),
            tag="NA",
            cal_score=0,
            short_risk=100,
            score_breakdown=bd,
        )
