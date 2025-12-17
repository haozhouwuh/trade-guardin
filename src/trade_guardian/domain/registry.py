from __future__ import annotations

from typing import Dict

from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.strategies.base import Strategy
from trade_guardian.strategies.calendar import CalendarStrategy
from trade_guardian.strategies.placeholder import PlaceholderStrategy


class StrategyRegistry:
    """
    Strategy registry for future expansion.
    Strategy #2/#3 will register here without touching app/orchestrator.
    """
    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        self.cfg = cfg
        self.policy = policy
        self._strategies: Dict[str, Strategy] = {}

        # Built-ins
        self.register("calendar", CalendarStrategy(cfg, policy))

        # Placeholder hook: keeps the framework ready, but doesn't implement logic.
        # You can remove this registration if you don't want it visible.
        self.register("placeholder", PlaceholderStrategy(cfg, policy))

    def register(self, name: str, strategy: Strategy) -> None:
        self._strategies[name] = strategy

    def get(self, name: str) -> Strategy:
        if name not in self._strategies:
            raise KeyError(f"Unknown strategy '{name}'. Available: {', '.join(sorted(self._strategies.keys()))}")
        return self._strategies[name]

    def names(self):
        return sorted(self._strategies.keys())
