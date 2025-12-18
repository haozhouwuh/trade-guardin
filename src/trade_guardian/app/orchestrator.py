from __future__ import annotations

import os
from typing import List, Tuple, Dict

from trade_guardian.infra.rate_limit import RateLimiter
from trade_guardian.infra.tickers import load_tickers_csv
from trade_guardian.infra.cache import JsonDailyCache
from trade_guardian.infra.schwab_client import SchwabClient

from trade_guardian.domain.models import Context, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.domain.features import TSFeatureBuilder
from trade_guardian.domain.hv import HVService

from trade_guardian.app.renderer import ScanlistRenderer
from trade_guardian.strategies.base import Strategy


class TradeGuardian:
    """
    Orchestrates:
      - load universe (tickers.csv)
      - fetch market/option data via SchwabClient
      - compute HV + term structure features
      - run strategy scoring + risk
      - render results
    """

    def __init__(self, client: SchwabClient, cfg: dict, policy: ShortLegPolicy, strategy: Strategy):
        self.client = client
        self.cfg = cfg
        self.policy = policy
        self.strategy = strategy

        cache_dir = cfg["paths"]["cache_dir"]
        os.makedirs(cache_dir, exist_ok=True)
        hv_cache_path = os.path.join(cache_dir, "hv_cache.json")

        self.hv_cache = JsonDailyCache(hv_cache_path)
        self.hv_service = HVService(client, self.hv_cache)

        self.tsf_builder = TSFeatureBuilder(cfg, policy)
        self.limiter = RateLimiter(cfg["scan"]["throttle_sec"])

        self.renderer = ScanlistRenderer(cfg, policy, hv_cache_path=hv_cache_path)

    def _build_context(self, symbol: str, days: int, base_rank: int) -> Context:
        vix = self.client.get_market_vix()
        hv = self.hv_service.get_hv(symbol)

        price, term = self.client.scan_atm_term(
            symbol,
            days,
            contract_type=self.cfg["scan"]["contract_type"],
        )
        tsf = self.tsf_builder.build(term, hv, rank=base_rank)
        if tsf.get("status") != "Success":
            raise RuntimeError(tsf.get("msg", "TSF error"))

        return Context(symbol=symbol, price=price, vix=vix, term=term, hv=hv, tsf=tsf)

    def scanlist(self, days: int, min_score: int, max_risk: int, limit: int = 0, detail: bool = False):
        """
        Strict rule (MUST be enforced exactly):
            cal_score >= min_score AND short_risk <= max_risk
        Watch:
            cal_score >= min_score AND short_risk > max_risk
        """
        csv_path = self.cfg["paths"]["tickers_csv"]
        tickers = load_tickers_csv(csv_path)
        if limit and limit > 0:
            tickers = tickers[:limit]

        base_rank = int(self.policy.base_rank)

        rows_ctx: List[Tuple[ScanRow, Context]] = []
        errors: List[Tuple[str, str]] = []

        for sym in tickers:
            try:
                ctx = self._build_context(sym, days, base_rank)
                row = self.strategy.evaluate(ctx)  # type: ignore
                rows_ctx.append((row, ctx))
            except Exception as e:
                errors.append((sym, str(e)))
            finally:
                self.limiter.sleep()

        rows: List[ScanRow] = [rc[0] for rc in rows_ctx]
        ctx_map: Dict[str, Context] = {rc[0].symbol: rc[1] for rc in rows_ctx}

        # ------------------------------------------------------------------
        # STRICT/WATCH FILTERS (IMPORTANT: strict must use <= max_risk exactly)
        # ------------------------------------------------------------------
        strict: List[ScanRow] = [
            r for r in rows
            if (r.cal_score >= min_score) and (r.short_risk <= max_risk)
        ]

        watch_candidates: List[ScanRow] = [
            r for r in rows
            if (r.cal_score >= min_score) and (r.short_risk > max_risk)
        ]

        auto_adjusted: List[ScanRow] = []
        still_watch: List[ScanRow] = []

        # Try auto-adjust within probe ranks only for the watch candidates
        for r in watch_candidates:
            ctx = ctx_map.get(r.symbol)
            if not ctx:
                still_watch.append(r)
                continue

            rec, summary = self.strategy.recommend(ctx, min_score=min_score, max_risk=max_risk)  # type: ignore
            if rec:
                r.rec = rec
                r.probe_summary = summary
                auto_adjusted.append(r)
            else:
                r.probe_summary = summary
                still_watch.append(r)

        # sorting helpers
        def _sort_key_score(r: ScanRow):
            return (r.cal_score, r.edge, -r.short_risk)

        def _sort_key_rec(r: ScanRow):
            if not r.rec:
                return (0, 0.0, -100)
            return (r.rec.rec_score, r.rec.rec_edge, -(r.rec.rec_risk))

        strict.sort(key=_sort_key_score, reverse=True)
        auto_adjusted.sort(key=_sort_key_rec, reverse=True)
        still_watch.sort(key=_sort_key_score, reverse=True)
        top = sorted(rows, key=_sort_key_score, reverse=True)

        # Renderer is responsible for printing thresholds consistently.
        # We pass the same min_score/max_risk used above, so “printed rule” == “actual rule”.
        self.renderer.render(
            days=days,
            universe_size=len(rows),
            min_score=min_score,
            max_risk=max_risk,
            strict=strict,
            auto_adjusted=auto_adjusted,
            watch=still_watch,
            top=top,
            errors=errors,
            detail=detail,
        )

        self.renderer.render_diagnostics(
            rows=rows,
            min_score=min_score,
            max_risk=max_risk,
            strict=strict,
            auto_adjusted=auto_adjusted,
            detail=detail,
        )
