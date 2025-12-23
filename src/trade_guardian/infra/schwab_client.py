from __future__ import annotations

import requests
import numpy as np
import pandas as pd

from datetime import datetime, timedelta, date
from urllib.parse import quote
from typing import Optional, Any, List, Dict, Tuple

from trade_guardian.domain.models import Context, IVData, HVInfo, TermPoint
from trade_guardian.infra.schwab_token_manager import fetch_schwab_token


# =========================================================
# Helpers
# =========================================================

def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()


def is_third_friday(d: date) -> bool:
    # Third Friday: weekday=4 (Fri) and day 15..21
    return d.weekday() == 4 and 15 <= d.day <= 21


def get_series_kind(exp_str: str) -> str:
    d = _to_date(exp_str)
    if is_third_friday(d):
        return "MONTHLY"
    if d.weekday() == 4:
        return "WEEKLY"
    return "DAILY"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _pick_iv(quote_obj: Dict[str, Any]) -> float:
    """
    Schwab API 在不同端点/字段可能有差异：优先兼容多个 key。
    注意：这里拿到的可能是 0.32 (32%) 或 32.0 (32%)，单位统一在 build_context 里做 heuristic。
    """
    for k in ("volatility", "impliedVolatility", "iv", "impliedVol"):
        v = quote_obj.get(k, None)
        iv = _safe_float(v, 0.0)
        if iv > 0:
            return iv
    return 0.0


def _pick_mark(quote_obj: Dict[str, Any]) -> float:
    # 更硬的 mid fallback：mark -> (bid+ask)/2 -> last -> 0
    mark = _safe_float(quote_obj.get("mark", None), 0.0)
    if mark > 0:
        return mark
    bid = _safe_float(quote_obj.get("bid", None), 0.0)
    ask = _safe_float(quote_obj.get("ask", None), 0.0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = _safe_float(quote_obj.get("last", None), 0.0)
    return last if last > 0 else 0.0


# =========================================================
# SchwabClient
# =========================================================

class SchwabClient:
    OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
    QUOTE_URL_TEMPLATE = "https://api.schwabapi.com/marketdata/v1/quotes?symbols={symbols}&fields=quote"
    PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    # ----------------------------
    # Basic API
    # ----------------------------

    def _headers(self) -> Dict[str, str]:
        token = fetch_schwab_token()
        if not token:
            raise ValueError("Token fetch failed")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_quote(self, symbol: str) -> dict:
        encoded = quote(symbol, safe="")
        url = self.QUOTE_URL_TEMPLATE.format(symbols=encoded)
        resp = requests.get(url, headers=self._headers(), timeout=20)
        data = resp.json()
        return data.get(symbol, {}).get("quote", {}) or {}

    def calculate_hv_percentile(self, symbol: str) -> HVInfo:
        try:
            params = {
                "symbol": symbol,
                "periodType": "year",
                "period": 2,
                "frequencyType": "daily",
                "frequency": 1,
            }
            resp = requests.get(self.PRICE_HISTORY_URL, headers=self._headers(), params=params, timeout=30)
            data = resp.json()
            candles = data.get("candles") or []
            if not candles:
                return HVInfo(status="Error")

            df = pd.DataFrame(candles)
            df["close"] = df["close"].astype(float)
            df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
            df["hv"] = df.dropna(subset=["log_ret"])["log_ret"].rolling(window=20).std() * np.sqrt(252) * 100

            current_hv = float(df["hv"].iloc[-1])
            recent = df["hv"].dropna().tail(252)
            hv_low, hv_high = float(recent.min()), float(recent.max())
            hv_rank = (current_hv - hv_low) / (hv_high - hv_low) * 100.0 if hv_high > hv_low else 0.0

            return HVInfo(status="Success", current_hv=current_hv, hv_rank=hv_rank, hv_low=hv_low, hv_high=hv_high)
        except Exception:
            return HVInfo(status="Error")

    def _fetch_chain(self, symbol: str, from_d: str, to_d: str, range_val: str = "ALL") -> dict:
        params = {
            "symbol": symbol,
            "contractType": "ALL",
            "strategy": "SINGLE",
            "range": range_val,
            "fromDate": from_d,
            "toDate": to_d,
        }
        resp = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
        return resp.json() if resp.status_code == 200 else {}

    # ----------------------------
    # Term scan (FIXED)
    # ----------------------------

    def scan_atm_term(self, symbol: str, days: int) -> Tuple[float, List[TermPoint], dict]:
        """
        目标：扫描 term structure 点用于 TSF 计算。
        ✅ FIX：每个 expiry 不再只取“最接近现价那一档”，而是取离现价最近的前 N 档，找到第一个 IV>0 的。
        这样不会把 30-45DTE 批量误杀，避免 month_exp 被 fallback 到 87/98DTE。
        """
        q = self.get_quote(symbol)
        price = _safe_float(q.get("lastPrice") or q.get("last") or q.get("mark"), 0.0)
        if price <= 0:
            raise RuntimeError(f"No price for {symbol}")

        from_date = datetime.now().strftime("%Y-%m-%d")
        to_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        chain = self._fetch_chain(symbol, from_date, to_date, range_val="ALL")
        call_map = chain.get("callExpDateMap") or {}

        term: List[TermPoint] = []
        TOP_N_STRIKES = int(self.cfg.get("scan", {}).get("atm_probe_strikes", 6) or 6)

        for date_str, strikes_map in sorted(call_map.items()):
            try:
                parts = date_str.split(":")
                date_iso, dte = parts[0], int(parts[1])
            except Exception:
                continue

            if not isinstance(strikes_map, dict) or not strikes_map:
                continue

            # 1) strikes 按离现价距离排序，取前 N 个
            strike_items = list(strikes_map.items())

            def _dist(item) -> float:
                try:
                    s_val = float(item[0])
                    return abs(s_val - price)
                except Exception:
                    return 1e18

            strike_items.sort(key=_dist)
            strike_items = strike_items[:max(1, TOP_N_STRIKES)]

            best_data = None
            best_strike = 0.0

            # 2) 在前 N 个里找第一个 iv>0 的
            for s_str, contracts in strike_items:
                if not contracts:
                    continue
                c = contracts[0] or {}
                iv = _pick_iv(c)
                if iv <= 0:
                    continue

                best_strike = _safe_float(s_str, 0.0)
                best_data = {
                    "iv": iv,
                    "mark": _pick_mark(c),
                    "delta": _safe_float(c.get("delta"), 0.0),
                    "theta": _safe_float(c.get("theta"), 0.0),
                    "gamma": _safe_float(c.get("gamma"), 0.0),
                }
                break

            if best_data and best_strike > 0:
                term.append(
                    TermPoint(
                        exp=date_iso,
                        exp_date=date_iso,
                        dte=dte,
                        strike=best_strike,
                        iv=best_data["iv"],
                        mark=best_data["mark"],
                        delta=best_data["delta"],
                        theta=best_data["theta"],
                        gamma=best_data["gamma"],
                    )
                )

        return price, term, chain

    # ----------------------------
    # Core: build_context (Anchor vs DiagLong split)
    # ----------------------------

    def _rules(self) -> dict:
        return self.cfg.get("rules", {}) or {}

    def _rget(self, key: str, default: Any) -> Any:
        return self._rules().get(key, default)

    def _select_anchor_point(self, term_points: List[TermPoint], short_point: TermPoint) -> TermPoint:
        """
        Anchor（月度锚点）：用于 Edge/Shape 的稳定参考点。
        默认窗口：anchor_min_dte..anchor_max_dte，fallback 扩到 anchor_fallback_max_dte。
        """
        min_dte = int(self._rget("anchor_min_dte", self._rget("month_min_dte", 20)))
        max_dte = int(self._rget("anchor_max_dte", self._rget("month_max_dte", 45)))
        fb_max = int(self._rget("anchor_fallback_max_dte", self._rget("month_fallback_max_dte", 90)))
        target = float(self._rget("anchor_target_dte", self._rget("month_target_dte", 35)))
        lam = float(self._rget("anchor_lambda_dist", self._rget("month_lambda_dist", 0.35)))
        prefer_monthly = bool(self._rget("anchor_prefer_monthly", self._rget("month_prefer_monthly", True)))

        pool = [p for p in term_points if min_dte <= p.dte <= max_dte]
        if len(pool) < 3:
            pool = [p for p in term_points if min_dte <= p.dte <= fb_max]

        if len(pool) < 3:
            # 极端退化：选 dte>=min_dte 中离 target 最近
            cand = [p for p in term_points if p.dte >= min_dte]
            if cand:
                return min(cand, key=lambda p: abs(p.dte - target))
            return term_points[-1]

        scored: List[Tuple[float, float, TermPoint]] = []
        for i in range(1, len(pool) - 1):
            window = [pool[i - 1].iv, pool[i].iv, pool[i + 1].iv]
            sd = float(np.std(window))
            dist_penalty = abs(pool[i].dte - target) / max(1.0, target)
            score = sd + (lam * dist_penalty)
            scored.append((score, sd, pool[i]))

        scored.sort(key=lambda x: x[0])
        top = scored[: min(5, len(scored))]

        if prefer_monthly:
            for _, _, p in top:
                if get_series_kind(p.exp) == "MONTHLY":
                    return p
        return top[0][2]

    def _select_diag_long_point(self, term_points: List[TermPoint], short_point: TermPoint) -> TermPoint:
        """
        对角线 long leg：交易用真实长腿。
        ✅ 强约束：long_dte >= short_dte + diag_long_min_gap_vs_short
        默认窗口：diag_long_min_dte..diag_long_max_dte，fallback 扩到 diag_long_fallback_max_dte。
        """
        min_dte = int(self._rget("diag_long_min_dte", 45))
        max_dte = int(self._rget("diag_long_max_dte", 75))
        fb_max = int(self._rget("diag_long_fallback_max_dte", 120))
        target = float(self._rget("diag_long_target_dte", 60))
        lam = float(self._rget("diag_long_lambda_dist", 0.25))
        prefer_monthly = bool(self._rget("diag_long_prefer_monthly", False))
        min_gap = int(self._rget("diag_long_min_gap_vs_short", 20))

        min_needed = short_point.dte + max(0, min_gap)

        def eligible(p: TermPoint, hi: int) -> bool:
            return (min_dte <= p.dte <= hi) and (p.dte >= min_needed)

        pool = [p for p in term_points if eligible(p, max_dte)]
        if len(pool) < 3:
            pool = [p for p in term_points if eligible(p, fb_max)]

        if not pool:
            # 退化：找所有 >= min_needed 的里离 target 最近；再不行就最远
            cand = [p for p in term_points if p.dte >= min_needed]
            if cand:
                return min(cand, key=lambda p: abs(p.dte - target))
            return term_points[-1]

        # 用“距离 target 最小”为主，prefer_monthly 可选
        pool_sorted = sorted(pool, key=lambda p: abs(p.dte - target) + lam * abs(p.dte - target) / max(1.0, target))
        top = pool_sorted[: min(8, len(pool_sorted))]
        if prefer_monthly:
            for p in top:
                if get_series_kind(p.exp) == "MONTHLY":
                    return p
        return top[0]

    def build_context(self, symbol: str, days: int = 600) -> Optional[Context]:
        try:
            # 1) HV
            hv_info = self.calculate_hv_percentile(symbol)
            if getattr(hv_info, "status", "") == "Error":
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            # 2) Chain + term points
            price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            if not term_points or len(term_points) < 3:
                return None
            term_points.sort(key=lambda x: x.dte)

            # 3) IV 单位 heuristic：<1.5 视为小数 (0.32 => 32%)
            for p in term_points:
                if 0 < p.iv < 1.5:
                    p.iv *= 100.0

            # -----------------------------------------
            # A) Short leg selection (1..15d)
            # -----------------------------------------
            nearest_candidates = [p for p in term_points if p.dte >= 1]
            nearest_point = nearest_candidates[0] if nearest_candidates else term_points[0]

            base_rank = int(self.cfg.get("policy", {}).get("base_rank", 1) or 1)
            short_pool = [p for p in term_points if 1 <= p.dte <= 15]
            if short_pool:
                short_point = short_pool[base_rank] if len(short_pool) > base_rank else short_pool[-1]
            else:
                short_point = nearest_point

            short_iv_base = short_point.iv if short_point.iv > 0 else 1.0
            nearest_iv_base = nearest_point.iv if nearest_point.iv > 0 else 1.0

            # -----------------------------------------
            # B) Micro anchor (1..15d)
            # -----------------------------------------
            micro_pool = [p for p in term_points if 1 <= p.dte <= 15]
            micro_point = None

            if len(micro_pool) >= 2:
                local_maxima: List[TermPoint] = []
                for i in range(1, len(micro_pool) - 1):
                    if micro_pool[i].iv > micro_pool[i - 1].iv and micro_pool[i].iv > micro_pool[i + 1].iv:
                        local_maxima.append(micro_pool[i])

                if local_maxima:
                    micro_point = max(local_maxima, key=lambda x: x.iv)
                else:
                    def _momentum_score(p: TermPoint) -> float:
                        d_eff = max(1, p.dte)
                        return (p.iv - nearest_iv_base) / np.sqrt(d_eff)
                    micro_point = max(micro_pool, key=_momentum_score)

            if not micro_point:
                micro_point = short_point

            # -----------------------------------------
            # C) Anchor point (structure) + DiagLong point (trade)
            # -----------------------------------------
            anchor_point = self._select_anchor_point(term_points, short_point)
            diag_long_point = self._select_diag_long_point(term_points, short_point)

            # -----------------------------------------
            # 4) Assemble TSF
            # -----------------------------------------
            IV_FLOOR = 12.0

            # regime/curvature 仍然用 anchor_point 作为“月度锚”
            regime = "FLAT"
            if short_point.iv > anchor_point.iv * 1.03:
                regime = "BACKWARDATION"
            elif anchor_point.iv > short_point.iv * 1.03:
                regime = "CONTANGO"

            curvature = "SPIKY_FRONT" if micro_point.iv > short_point.iv * 1.10 else "NORMAL"
            is_squeeze = True if (micro_point.iv > anchor_point.iv * 1.05) else False

            tsf = {
                "regime": regime,
                "curvature": curvature,
                "is_squeeze": is_squeeze,

                # short / nearest / micro
                "short_exp": short_point.exp,
                "short_dte": short_point.dte,
                "short_iv": short_point.iv,
                "nearest_exp": nearest_point.exp,
                "nearest_dte": nearest_point.dte,
                "nearest_iv": nearest_point.iv,
                "micro_exp": micro_point.exp,
                "micro_dte": micro_point.dte,
                "micro_iv": micro_point.iv,

                # ✅ month_* = Anchor（结构锚点）
                "month_exp": anchor_point.exp,
                "month_dte": anchor_point.dte,
                "month_iv": anchor_point.iv,

                # ✅ diag_long_* = 交易用 long leg
                "diag_long_exp": diag_long_point.exp,
                "diag_long_dte": diag_long_point.dte,
                "diag_long_iv": diag_long_point.iv,

                # edges 使用 anchor_point
                "edge_micro": (micro_point.iv - short_point.iv) / max(IV_FLOOR, short_point.iv),
                "edge_month": (anchor_point.iv - short_point.iv) / max(IV_FLOOR, short_point.iv),
            }

            # 5) Build Context objects
            iv_data = IVData(
                rank=float(hv_info.hv_rank),
                percentile=0.0,
                current_iv=float(short_point.iv),
                hv_rank=float(hv_info.hv_rank),
                current_hv=float(hv_info.current_hv),
            )

            class Metrics:
                pass

            metrics = Metrics()
            metrics.gamma = float(short_point.gamma)
            metrics.delta = float(short_point.delta)
            metrics.theta = float(short_point.theta)

            return Context(
                symbol=symbol,
                price=float(price),
                iv=iv_data,
                hv=iv_data,
                tsf=tsf,
                raw_chain=raw_chain,
                metrics=metrics,
                term=term_points,
            )

        except Exception:
            return None
