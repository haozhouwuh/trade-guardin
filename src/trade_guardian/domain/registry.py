from __future__ import annotations

from trade_guardian.domain.policy import ShortLegPolicy

from trade_guardian.strategies.calendar import CalendarStrategy
from trade_guardian.strategies.hv_calendar import HVCalendarStrategy


class StrategyRegistry:
    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy

    def get(self, name: str):
        n = (name or "").strip().lower()

        if n in ("calendar", "cal"):
            return CalendarStrategy(self.cfg, self.policy)

        if n in ("hv_calendar", "hvcal", "hv"):
            return HVCalendarStrategy(self.cfg, self.policy)

        raise KeyError(f"Unknown strategy: {name}. Available: calendar, hv_calendar")
