from __future__ import annotations

from trade_guardian.domain.models import HVInfo
from trade_guardian.infra.cache import JsonDailyCache
from trade_guardian.infra.schwab_client import SchwabClient


class HVService:
    def __init__(self, client: SchwabClient, cache: JsonDailyCache):
        self.client = client
        self.cache = cache

    def get_hv(self, symbol: str) -> HVInfo:
        cached = self.cache.get(symbol)
        if cached:
            return HVInfo(**cached)

        hv = self.client.calculate_hv_percentile(symbol)
        # store even if partial to avoid repeated API spam
        self.cache.set(symbol, hv.__dict__)
        return hv
