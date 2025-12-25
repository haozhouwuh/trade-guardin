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
    Schwab chain contract:
      - volatility: 通常就是 IV（但 deep ITM/OTM 或 DTE<1 时可能失真到几百%）
      - theoreticalVolatility: Schwab 用于理论价的平滑波动率（常更稳定）

    规则（按你给的说明）：
      1) 优先 volatility
      2) 若 |delta|>0.90 或 <0.10，或 DTE<1，且 volatility 离谱(>100% 或 <1%)，
         且 theoreticalVolatility 正常，则回退 theoreticalVolatility
      3) volatility 缺失/为0 时，再尝试 impliedVolatility 等字段
    """
    raw_iv = _safe_float(quote_obj.get("volatility", None), 0.0)
    theo_iv = _safe_float(quote_obj.get("theoreticalVolatility", None), 0.0)

    delta = abs(_safe_float(quote_obj.get("delta", None), 0.0))
    dte = _safe_float(quote_obj.get("daysToExpiration", None), -1.0)  # chain 里一般有

    # 用与你 build_context 相同的 heuristic 转成“百分比尺度”来判断是否离谱
    def _as_pct(v: float) -> float:
        if 0 < v < 1.5:
            return v * 100.0
        return v

    raw_iv_pct = _as_pct(raw_iv)
    theo_iv_pct = _as_pct(theo_iv)

    extreme_delta = (delta > 0.90) or (0 < delta < 0.10)
    very_short_dte = (dte >= 0 and dte < 1)

    # 如果 volatility 缺失/为 0：先尝试其他 iv 字段
    if raw_iv <= 0:
        for k in ("impliedVolatility", "impliedVol", "iv"):
            v = _safe_float(quote_obj.get(k, None), 0.0)
            if v > 0:
                raw_iv = v
                raw_iv_pct = _as_pct(raw_iv)
                break

    # 关键清洗逻辑：deep ITM/OTM 或 DTE<1 时，volatility 可能反推崩溃
    if (extreme_delta or very_short_dte) and theo_iv > 0:
        if (raw_iv_pct > 100.0) or (0 < raw_iv_pct < 1.0):
            return theo_iv  # theo_iv 通常更稳定

    # 正常情况下：返回 volatility（或其 fallback 的 impliedVolatility）
    return raw_iv if raw_iv > 0 else 0.0


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
    # Term scan
    # ----------------------------

    def scan_atm_term(self, symbol: str, days: int) -> Tuple[float, List[TermPoint], dict]:
        """
        扫描 term structure 点用于 TSF。
        每个 expiry：取离现价最近的前 N 档，找到第一个 IV>0 的（避免把 30-45DTE 批量误杀）。
        """
        q = self.get_quote(symbol)
        price = _safe_float(q.get("lastPrice") or q.get("last") or q.get("mark"), 0.0)
        if price <= 0:
            raise RuntimeError(f"No price for {symbol}")

        from_date = datetime.now().strftime("%Y-%m-%d")
        to_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        chain = self._fetch_chain(symbol, from_date, to_date, range_val="ALL")
        call_map = chain.get("callExpDateMap") or {}

        # ============ [DEBUG START] ============
        # print(f"\n🔍 [DEBUG-DATA] {symbol}: Raw Chain Received.")
        # if not call_map:
        #     print(f"   ❌ Call Map is EMPTY! API returned: {str(chain)[:100]}...")
        # else:
        #     exp_dates = list(call_map.keys())
        #     print(f"   ✅ Found {len(exp_dates)} Expirations. Range: {exp_dates[0]} ~ {exp_dates[-1]}")
        # ============ [DEBUG END] ==============



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
        ✅ 修正：主窗口里“只要有点”就绝不 fallback 到更远；
               如果主窗口点数 <3，则不用三点曲率(sd)算法，改为按 target_dte 距离选（并尊重 prefer_monthly）。
        """
        min_dte = int(self._rget("anchor_min_dte", self._rget("month_min_dte", 20)))
        max_dte = int(self._rget("anchor_max_dte", self._rget("month_max_dte", 45)))
        fb_max = int(self._rget("anchor_fallback_max_dte", self._rget("month_fallback_max_dte", 90)))
        target = float(self._rget("anchor_target_dte", self._rget("month_target_dte", 35)))
        lam = float(self._rget("anchor_lambda_dist", self._rget("month_lambda_dist", 0.35)))
        prefer_monthly = bool(self._rget("anchor_prefer_monthly", self._rget("month_prefer_monthly", True)))

        pool = [p for p in term_points if min_dte <= int(p.dte) <= max_dte]

        if not pool:
            pool = [p for p in term_points if min_dte <= int(p.dte) <= fb_max]

        if not pool:
            cand = [p for p in term_points if int(p.dte) >= min_dte]
            if cand:
                return min(cand, key=lambda p: abs(int(p.dte) - target))
            return term_points[-1]

        if len(pool) < 3:
            candidates = pool
            if prefer_monthly:
                monthly = [p for p in candidates if get_series_kind(p.exp) == "MONTHLY"]
                if monthly:
                    candidates = monthly
            return min(candidates, key=lambda p: abs(int(p.dte) - target))

        scored: List[Tuple[float, float, TermPoint]] = []
        for i in range(1, len(pool) - 1):
            window = [pool[i - 1].iv, pool[i].iv, pool[i + 1].iv]
            sd = float(np.std(window))
            dist_penalty = abs(int(pool[i].dte) - target) / max(1.0, target)
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
        """
        min_dte = int(self._rget("diag_long_min_dte", 45))
        max_dte = int(self._rget("diag_long_max_dte", 75))
        fb_max = int(self._rget("diag_long_fallback_max_dte", 120))
        target = float(self._rget("diag_long_target_dte", 60))
        lam = float(self._rget("diag_long_lambda_dist", 0.25))
        prefer_monthly = bool(self._rget("diag_long_prefer_monthly", False))
        min_gap = int(self._rget("diag_long_min_gap_vs_short", 20))

        min_needed = int(short_point.dte) + max(0, min_gap)

        def eligible(p: TermPoint, hi: int) -> bool:
            return (min_dte <= int(p.dte) <= hi) and (int(p.dte) >= min_needed)

        pool = [p for p in term_points if eligible(p, max_dte)]
        if len(pool) < 3:
            pool = [p for p in term_points if eligible(p, fb_max)]

        if not pool:
            cand = [p for p in term_points if int(p.dte) >= min_needed]
            if cand:
                return min(cand, key=lambda p: abs(int(p.dte) - target))
            return term_points[-1]

        pool_sorted = sorted(
            pool,
            key=lambda p: abs(int(p.dte) - target) + lam * abs(int(p.dte) - target) / max(1.0, target),
        )
        top = pool_sorted[: min(8, len(pool_sorted))]
        if prefer_monthly:
            for p in top:
                if get_series_kind(p.exp) == "MONTHLY":
                    return p
        return top[0]

    def build_context(self, symbol: str, days: int = 600) -> Optional[Context]:
        try:
            hv_info = self.calculate_hv_percentile(symbol)
            if getattr(hv_info, "status", "") == "Error":
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            if not term_points or len(term_points) < 3:
                return None
            term_points.sort(key=lambda x: int(x.dte))

            for p in term_points:
                if 0 < float(p.iv) < 1.5:
                    p.iv *= 100.0

            nearest_candidates = [p for p in term_points if int(p.dte) >= 1]
            nearest_point = nearest_candidates[0] if nearest_candidates else term_points[0]

            base_rank = int(self.cfg.get("policy", {}).get("base_rank", 1) or 1)
            short_pool = [p for p in term_points if 1 <= int(p.dte) <= 15]
            if short_pool:
                short_point = short_pool[base_rank] if len(short_pool) > base_rank else short_pool[-1]
            else:
                short_point = nearest_point

            nearest_iv_base = float(nearest_point.iv) if float(nearest_point.iv) > 0 else 1.0

            micro_pool = [p for p in term_points if 1 <= int(p.dte) <= 15]
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
                        d_eff = max(1, int(p.dte))
                        return (float(p.iv) - nearest_iv_base) / np.sqrt(d_eff)
                    micro_point = max(micro_pool, key=_momentum_score)

            if not micro_point:
                micro_point = short_point

            anchor_point = self._select_anchor_point(term_points, short_point)
            diag_long_point = self._select_diag_long_point(term_points, short_point)

            IV_FLOOR = 12.0

            regime = "FLAT"
            if float(short_point.iv) > float(anchor_point.iv) * 1.03:
                regime = "BACKWARDATION"
            elif float(anchor_point.iv) > float(short_point.iv) * 1.03:
                regime = "CONTANGO"

            curvature = "SPIKY_FRONT" if float(micro_point.iv) > float(short_point.iv) * 1.10 else "NORMAL"
            is_squeeze = True if (float(micro_point.iv) > float(anchor_point.iv) * 1.05) else False

            tsf = {
                "regime": regime,
                "curvature": curvature,
                "is_squeeze": is_squeeze,

                "short_exp": short_point.exp,
                "short_dte": int(short_point.dte),
                "short_iv": float(short_point.iv),

                "nearest_exp": nearest_point.exp,
                "nearest_dte": int(nearest_point.dte),
                "nearest_iv": float(nearest_point.iv),

                "micro_exp": micro_point.exp,
                "micro_dte": int(micro_point.dte),
                "micro_iv": float(micro_point.iv),

                "month_exp": anchor_point.exp,
                "month_dte": int(anchor_point.dte),
                "month_iv": float(anchor_point.iv),

                "diag_long_exp": diag_long_point.exp,
                "diag_long_dte": int(diag_long_point.dte),
                "diag_long_iv": float(diag_long_point.iv),

                "edge_micro": (float(micro_point.iv) - float(short_point.iv)) / max(IV_FLOOR, float(short_point.iv)),
                "edge_month": (float(anchor_point.iv) - float(short_point.iv)) / max(IV_FLOOR, float(short_point.iv)),
            }

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
