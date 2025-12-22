from __future__ import annotations

from trade_guardian.domain.policy import ShortLegPolicy

from trade_guardian.strategies.auto import AutoStrategy
from trade_guardian.strategies.calendar import CalendarStrategy
from trade_guardian.strategies.hv_calendar import HVCalendarStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy
from trade_guardian.strategies.diagonal import DiagonalStrategy
from trade_guardian.strategies.iron_condor import IronCondorStrategy
from trade_guardian.strategies.vertical_credit import VerticalCreditStrategy


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
            
        if n in ("long_gamma", "gamma", "straddle", "lg"):
            return LongGammaStrategy(self.cfg, self.policy)
        
        if n in ("diagonal", "pmcc", "diag"):
            return DiagonalStrategy(self.cfg, self.policy)
        
        # [新增] IC 注册逻辑
        if n in ("ic", "condor", "iron_condor"):
            return IronCondorStrategy(self.cfg, self.policy)
        
        # [新增] 垂直价差
        if n in ("vertical", "pcs", "ccs", "credit_spread"):
            return VerticalCreditStrategy(self.cfg, self.policy)
        
        
        if n in ("auto", "smart", "default"):
            return AutoStrategy(self.cfg, self.policy)
        
        # 更新报错信息
        raise KeyError(f"Unknown strategy: {name}. Available: calendar, hv, lg, diagonal, ic, vertical, auto")