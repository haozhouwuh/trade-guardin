from __future__ import annotations

from trade_guardian.domain.policy import ShortLegPolicy

from trade_guardian.strategies.auto import AutoStrategy  # <--- 新增
from trade_guardian.strategies.calendar import CalendarStrategy
from trade_guardian.strategies.hv_calendar import HVCalendarStrategy
from trade_guardian.strategies.long_gamma import LongGammaStrategy



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
            
        # 这里注册关键字，让 CLI 能识别 "long_gamma"
        if n in ("long_gamma", "gamma", "straddle", "lg"):
            return LongGammaStrategy(self.cfg, self.policy)
        
        # 注册 auto
        if n in ("auto", "smart", "default"):
            return AutoStrategy(self.cfg, self.policy)

        # 更新报错信息，提示用户支持的新策略
        raise KeyError(f"Unknown strategy: {name}. Available: calendar, hv_calendar, long_gamma")