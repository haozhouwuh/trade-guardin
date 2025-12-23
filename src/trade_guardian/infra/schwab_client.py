from __future__ import annotations

import requests
import numpy as np
import pandas as pd
import traceback
from datetime import datetime, timedelta, date
from urllib.parse import quote
from typing import Optional, Any, List

from trade_guardian.domain.models import Context, IVData, HVInfo, TermPoint
from trade_guardian.infra.schwab_token_manager import fetch_schwab_token

# --- Helper Functions ---
def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()

def is_third_friday(d: date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21

def get_series_kind(exp_str: str) -> str:
    d = _to_date(exp_str)
    if is_third_friday(d): return "MONTHLY"
    if d.weekday() == 4: return "WEEKLY"
    return "DAILY"

class SchwabClient:
    OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
    QUOTE_URL_TEMPLATE = "https://api.schwabapi.com/marketdata/v1/quotes?symbols={symbols}&fields=quote"
    PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or {}

    def build_context(self, symbol: str, days: int = 600) -> Optional[Context]:
        try:
            # 1. 获取 HV
            hv_info = self.calculate_hv_percentile(symbol)
            if getattr(hv_info, 'status', '') == "Error":
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            # 2. 获取期权链
            try:
                price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            except Exception:
                return None

            if not term_points or len(term_points) < 3: return None
            term_points.sort(key=lambda x: x.dte)

            # [FIX] IV 单位更稳健的 heuristic (< 1.5 视为小数)
            for p in term_points:
                if 0 < p.iv < 1.5: 
                    p.iv *= 100.0

            # -----------------------------------------------------------
            # 算法 A: 确定 Short Legs (双轨制)
            # -----------------------------------------------------------
            nearest_candidates = [p for p in term_points if p.dte >= 1]
            nearest_point = nearest_candidates[0] if nearest_candidates else term_points[0]

            base_rank = self.cfg.get("policy", {}).get("base_rank", 1)
            short_pool = [p for p in term_points if 1 <= p.dte <= 15]
            
            if len(short_pool) > base_rank:
                short_point = short_pool[base_rank] 
            else:
                short_point = short_pool[-1] if short_pool else nearest_point

            short_iv_base = short_point.iv if short_point.iv > 0 else 1.0
            nearest_iv_base = nearest_point.iv if nearest_point.iv > 0 else 1.0

            # -----------------------------------------------------------
            # 算法 B: 寻找 Micro Anchor
            # -----------------------------------------------------------
            micro_pool = [p for p in term_points if 1 <= p.dte <= 15]
            micro_point = None
            
            if len(micro_pool) >= 2:
                local_maxima = []
                for i in range(1, len(micro_pool) - 1):
                    if micro_pool[i].iv > micro_pool[i-1].iv and micro_pool[i].iv > micro_pool[i+1].iv:
                        local_maxima.append(micro_pool[i])
                if local_maxima:
                    micro_point = max(local_maxima, key=lambda x: x.iv)
                else:
                    def _momentum_score(p):
                        d_eff = max(1, p.dte)
                        return (p.iv - nearest_iv_base) / np.sqrt(d_eff)
                    micro_point = max(micro_pool, key=_momentum_score)
            
            if not micro_point: micro_point = short_point

            # -----------------------------------------------------------
            # 算法 C: 寻找 Month Anchor
            # -----------------------------------------------------------
            month_pool = [p for p in term_points if 25 <= p.dte <= 45]
            if len(month_pool) < 3: month_pool = [p for p in term_points if 25 <= p.dte <= 90]
            
            month_point = None
            TARGET_MONTH = 35.0
            LAMBDA_DIST = 0.35 

            if len(month_pool) >= 3:
                scored_candidates = []
                for i in range(1, len(month_pool) - 1):
                    window = [month_pool[i-1].iv, month_pool[i].iv, month_pool[i+1].iv]
                    sd = np.std(window)
                    dist_penalty = abs(month_pool[i].dte - TARGET_MONTH) / TARGET_MONTH
                    score = sd + (LAMBDA_DIST * dist_penalty)
                    scored_candidates.append((score, sd, month_pool[i]))
                
                scored_candidates.sort(key=lambda x: x[0])
                top_5 = scored_candidates[:min(5, len(scored_candidates))]
                
                best_monthly = next((p for sc, sd, p in top_5 if get_series_kind(p.exp) == "MONTHLY"), None)
                month_point = best_monthly if best_monthly else top_5[0][2]
            else:
                fallback_candidates = [p for p in term_points if p.dte >= 20]
                if fallback_candidates:
                    month_point = min(fallback_candidates, key=lambda x: abs(x.dte - 30))
                else:
                    month_point = term_points[-1]

            # -----------------------------------------------------------
            # 4. 数据组装
            # -----------------------------------------------------------
            IV_FLOOR = 12.0
            
            tsf = {
                "regime": "BACKWARDATION" if short_point.iv > month_point.iv * 1.03 else ("CONTANGO" if month_point.iv > short_point.iv * 1.03 else "FLAT"),
                "curvature": "SPIKY_FRONT" if micro_point.iv > short_point.iv * 1.10 else "NORMAL",
                "is_squeeze": True if (micro_point.iv > month_point.iv * 1.05) else False,

                "short_exp": short_point.exp, "short_dte": short_point.dte, "short_iv": short_point.iv, 
                "nearest_exp": nearest_point.exp, "nearest_dte": nearest_point.dte, "nearest_iv": nearest_point.iv,
                "micro_exp": micro_point.exp, "micro_dte": micro_point.dte, "micro_iv": micro_point.iv,
                "month_exp": month_point.exp, "month_dte": month_point.dte, "month_iv": month_point.iv,
                
                "edge_micro": (micro_point.iv - short_point.iv) / max(IV_FLOOR, short_point.iv),
                "edge_month": (month_point.iv - short_point.iv) / max(IV_FLOOR, short_point.iv),
            }

            iv_data = IVData(rank=hv_info.hv_rank, percentile=0.0, current_iv=short_point.iv, hv_rank=hv_info.hv_rank, current_hv=hv_info.current_hv)
            class Metrics: pass
            metrics = Metrics(); metrics.gamma = short_point.gamma; metrics.delta = short_point.delta; metrics.theta = short_point.theta

            return Context(symbol, price, iv_data, iv_data, tsf, raw_chain, metrics, term_points)

        except Exception as e:
            return None

    # --- 基础 API ---
    def _headers(self):
        token = fetch_schwab_token()
        if not token: raise ValueError("Token fetch failed")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_quote(self, symbol: str) -> dict:
        encoded = quote(symbol, safe="")
        url = self.QUOTE_URL_TEMPLATE.format(symbols=encoded)
        resp = requests.get(url, headers=self._headers(), timeout=20)
        return resp.json().get(symbol, {}).get("quote", {}) or {}

    def calculate_hv_percentile(self, symbol: str) -> HVInfo:
        try:
            params = {"symbol": symbol, "periodType": "year", "period": 2, "frequencyType": "daily", "frequency": 1}
            resp = requests.get(self.PRICE_HISTORY_URL, headers=self._headers(), params=params, timeout=30)
            data = resp.json()
            candles = data.get("candles") or []
            if not candles: return HVInfo(status="Error")

            df = pd.DataFrame(candles)
            df["close"] = df["close"].astype(float)
            df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
            df["hv"] = df.dropna(subset=["log_ret"])["log_ret"].rolling(window=20).std() * np.sqrt(252) * 100
            
            current_hv = float(df["hv"].iloc[-1])
            recent = df["hv"].dropna().tail(252)
            hv_low, hv_high = float(recent.min()), float(recent.max())
            hv_rank = (current_hv - hv_low) / (hv_high - hv_low) * 100.0 if hv_high > hv_low else 0.0
            
            return HVInfo(status="Success", current_hv=current_hv, hv_rank=hv_rank, hv_low=hv_low, hv_high=hv_high)
        except: return HVInfo(status="Error")

    def _fetch_calls_chain(self, symbol: str, from_d: str, to_d: str, range_val: str = "NTM") -> dict:
        params = {"symbol": symbol, "contractType": "ALL", "strategy": "SINGLE", "range": range_val, "fromDate": from_d, "toDate": to_d}
        resp = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
        return resp.json() if resp.status_code == 200 else {}

    def scan_atm_term(self, symbol: str, days: int) -> tuple[float, list[TermPoint], dict]:
        """
        [FIX] 这里的逻辑是扫描 'ATM' 用来画曲线。
        Pin 风险推算 Strikes 需要更多数据，但不能在这里让 scan_atm_term 返回巨量数据拖慢速度。
        所以 Pin 风险逻辑要在策略层直接读 raw_chain.
        """
        q = self.get_quote(symbol)
        price = float(q.get("lastPrice") or 0.0)
        if price == 0: raise RuntimeError(f"No price for {symbol}")

        from_date = datetime.now().strftime("%Y-%m-%d")
        to_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        chain = self._fetch_calls_chain(symbol, from_date, to_date, range_val="ALL")
        call_map = chain.get("callExpDateMap") or {}

        term: list[TermPoint] = []
        for date_str, strikes_map in sorted(call_map.items()):
            parts = date_str.split(":")
            date_iso, dte = parts[0], int(parts[1])
            
            best_strike, min_dist, best_data = 0.0, 1e18, None
            for s_str, contracts in strikes_map.items():
                s_val = float(s_str)
                dist = abs(s_val - price)
                if dist < min_dist:
                    c = contracts[0]
                    iv = float(c.get("volatility", 0) or 0.0)
                    if iv > 0:
                        min_dist, best_strike = dist, s_val
                        best_data = {
                            "iv": iv, "mark": float(c.get("mark") or 0.0), 
                            "delta": float(c.get("delta") or 0.0), 
                            "theta": float(c.get("theta") or 0.0), 
                            "gamma": float(c.get("gamma") or 0.0)
                        }
            if best_data:
                term.append(TermPoint(exp=date_iso, dte=dte, strike=best_strike, **best_data))
        
        return price, term, chain