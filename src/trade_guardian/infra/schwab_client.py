from __future__ import annotations

import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from urllib.parse import quote

from trade_guardian.domain.models import HVInfo, TermPoint
from trade_guardian.infra.schwab_token_manager import fetch_schwab_token


class SchwabClient:
    OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
    QUOTE_URL_TEMPLATE = "https://api.schwabapi.com/marketdata/v1/quotes?symbols={symbols}&fields=quote"
    PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _headers(self):
        token = fetch_schwab_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_quote(self, symbol: str) -> dict:
        encoded = quote(symbol, safe="")
        url = self.QUOTE_URL_TEMPLATE.format(symbols=encoded)
        resp = requests.get(url, headers=self._headers(), timeout=20)
        if resp.status_code != 200:
            return {}
        return resp.json().get(symbol, {}).get("quote", {}) or {}

    def get_market_vix(self) -> float:
        q = self.get_quote("$VIX")
        price = q.get("lastPrice") or q.get("closePrice") or q.get("mark")
        try:
            return float(price) if price else 0.0
        except Exception:
            return 0.0

    def calculate_hv_percentile(self, symbol: str) -> HVInfo:
        try:
            params = {"symbol": symbol, "periodType": "year", "period": 2, "frequencyType": "daily", "frequency": 1}
            resp = requests.get(self.PRICE_HISTORY_URL, headers=self._headers(), params=params, timeout=30)
            if resp.status_code != 200:
                return HVInfo(status="Error", msg=f"API Error {resp.status_code}")

            data = resp.json()
            candles = data.get("candles") or []
            if not candles:
                return HVInfo(status="Error", msg="No candles")

            df = pd.DataFrame(candles)
            df["close"] = df["close"].astype(float)
            df = df.sort_index(ascending=True)
            df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
            df = df.dropna(subset=["log_ret"]).copy()
            df = df[df["log_ret"].abs() > 1e-8]
            df["hv"] = df["log_ret"].rolling(window=20).std(ddof=1) * np.sqrt(252) * 100
            hv_series = df["hv"].dropna()
            if len(hv_series) < 10:
                return HVInfo(status="Error", msg="Not enough data")

            lookback = 252
            recent = hv_series.tail(lookback) if len(hv_series) >= lookback else hv_series
            current_hv = float(recent.iloc[-1])
            hv_low = float(recent.min())
            hv_high = float(recent.max())

            p50 = float(np.percentile(recent, 50))
            p75 = float(np.percentile(recent, 75))
            p90 = float(np.percentile(recent, 90))

            hv_rank = 0.0 if hv_high == hv_low else (current_hv - hv_low) / (hv_high - hv_low) * 100.0
            return HVInfo(
                status="Success",
                current_hv=current_hv,
                hv_rank=hv_rank,
                hv_low=hv_low,
                hv_high=hv_high,
                p50=p50,
                p75=p75,
                p90=p90,
            )
        except Exception as e:
            return HVInfo(status="Error", msg=str(e))

    def _fetch_calls_chain(self, symbol: str, from_d: str, to_d: str, range_val: str = "ATM") -> dict:   
        params = {
            "symbol": symbol,
            "contractType": "CALL",
            "includeUnderlyingQuote": "true",
            "strategy": "SINGLE",
            "range": range_val,
            "fromDate": from_d,
            "toDate": to_d,
        }
        resp = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 400 and range_val == "ALL":
            params["range"] = "ATM"
            retry = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
            if retry.status_code == 200:
                return retry.json()
        return {}

    def scan_atm_term(self, symbol: str, days: int, contract_type: str = "CALL") -> tuple[float, list[TermPoint], dict]:
        q = self.get_quote(symbol)
        price = q.get("lastPrice") or q.get("closePrice") or q.get("mark")
        if not price:
            raise RuntimeError("Could not get stock price.")
        price = float(price)

        from_date = datetime.now().strftime("%Y-%m-%d")
        to_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        # ... (获取 chain 的代码)
        #chain = self._fetch_calls_chain(symbol, from_date, to_date, range_val="ATM")
        #chain = self._fetch_calls_chain(symbol, from_date, to_date, range_val="NTM")
        chain = self._fetch_calls_chain(symbol, from_date, to_date, range_val="ALL")

        call_map = chain.get("callExpDateMap") or {}
        if not call_map:
            raise RuntimeError("No option chain data returned.")

        dates = sorted(list(call_map.keys()))
        term: list[TermPoint] = []

        for date_str in dates:
            parts = date_str.split(":")
            date_iso = parts[0]
            try:
                dte = int(parts[1])
            except Exception:
                dte = (datetime.strptime(date_iso, "%Y-%m-%d") - datetime.now()).days

            strikes_map = call_map[date_str]
            min_dist = 1e18
            best: dict | None = None
            best_strike = 0.0

            for s_str, contracts in strikes_map.items():
                try:
                    s_val = float(s_str)
                    dist = abs(s_val - price)
                    if dist < min_dist:
                        c = contracts[0]
                        iv = float(c.get("volatility", 0) or 0.0)
                        mark = float(c.get("mark") or c.get("closePrice") or 0.0)
                        delta = float(c.get("delta") or 0.0)
                        theta = float(c.get("theta") or 0.0)
                        gamma = float(c.get("gamma") or 0.0)
                        if iv > 0:
                            if iv < 5.0:
                                iv *= 100.0
                            min_dist = dist
                            best = {"iv": iv, "mark": mark, "delta": delta, "theta": theta, "gamma": gamma}
                            best_strike = s_val
                except Exception:
                    continue

            if best:
                term.append(
                    TermPoint(
                        exp=date_iso,
                        dte=int(dte),
                        strike=float(best_strike),
                        mark=float(best["mark"]),
                        iv=float(best["iv"]),
                        delta=float(best["delta"]),
                        theta=float(best["theta"]),
                        gamma=float(best["gamma"]),
                    )
                )

        if not term:
            raise RuntimeError("Could not build term points.")

        return price, term, chain
