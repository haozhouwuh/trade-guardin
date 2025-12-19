from __future__ import annotations

import requests
import numpy as np
import pandas as pd
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Optional, Any

from trade_guardian.domain.models import Context, IVData, HVInfo, TermPoint
from trade_guardian.infra.schwab_token_manager import fetch_schwab_token

class SchwabClient:
    OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
    QUOTE_URL_TEMPLATE = "https://api.schwabapi.com/marketdata/v1/quotes?symbols={symbols}&fields=quote"
    PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or {}

    def build_context(self, symbol: str, days: int = 600) -> Optional[Context]:
        """
        Orchestrator 调用的标准入口。
        负责获取 Price, IV, HV, Option Chain 并组装成 Context 对象。
        """
        try:
            # 1. 获取 HV 数据
            hv_info = self.calculate_hv_percentile(symbol)
            if getattr(hv_info, 'status', '') == "Error":
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            # 2. 获取 Term Structure 和 Chain
            try:
                price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            except Exception as e:
                print(f"  [Warn] Chain scan failed for {symbol}: {e}")
                return None

            if not term_points:
                return None

            # 3. 分析期限结构 (Term Structure)
            term_points.sort(key=lambda x: x.dte)

            # --- [逻辑修正：释放天期权灵敏度] ---
            # A. 寻找 Short Term (目标：1-10 DTE 范围内的最近有效合约)
            short_candidates = [p for p in term_points if 1 <= p.dte <= 10]
            
            if not short_candidates:
                # 如果 10 天内没有任何期权，选择全场最近的
                short_point = min(term_points, key=lambda x: x.dte)
            else:
                # 核心逻辑：选出范围内 DTE 最小的合约，避开 0DTE (当天到期)
                # 这样 SPY/QQQ 会选到 1-3 天，而 NVDA 会选到 7 天
                short_point = min(short_candidates, key=lambda x: x.dte if x.dte > 0 else 999)
            
            # B. 寻找 Base Term (目标：45-60 DTE 作为基准锚点)
            base_candidates = [p for p in term_points if 40 <= p.dte <= 90]
            if not base_candidates:
                base_point = min(term_points, key=lambda x: abs(x.dte - 60))
            else:
                base_point = min(base_candidates, key=lambda x: abs(x.dte - 50))
            # ------------------------------------

            # 4. 确定 IV 和 Regime
            short_iv = short_point.iv
            base_iv = base_point.iv
            
            if base_iv == 0: base_iv = short_iv if short_iv > 0 else 1.0

            # 判断期限结构状态
            regime = "CONTANGO" if base_iv > short_iv else "BACKWARDATION"
            if abs(base_iv - short_iv) < 1.5:
                regime = "NORMAL"

            # 5. 组装 IVData
            iv_data = IVData(
                rank=hv_info.hv_rank,
                percentile=0.0,
                current_iv=short_iv,
                hv_rank=hv_info.hv_rank,
                current_hv=hv_info.current_hv
            )

            # 6. 组装 TSF (Term Structure Factors)
            tsf = {
                "regime": regime,
                "curvature": "FLAT",
                "short_exp": short_point.exp,
                "short_dte": short_point.dte,
                "short_iv": short_iv,
                "base_iv": base_iv
            }

            # 7. 组装 Metrics (Greeks)
            class Metrics: pass
            metrics = Metrics()
            metrics.gamma = short_point.gamma
            metrics.delta = short_point.delta
            metrics.theta = short_point.theta

            # 8. 返回 Context
            return Context(
                symbol=symbol,
                price=price,
                iv=iv_data,
                hv=iv_data, 
                tsf=tsf,
                raw_chain=raw_chain,
                metrics=metrics
            )

        except Exception as e:
            print(f"❌ [Error] build_context critical fail for {symbol}: {e}")
            return None

    # --- 基础 API 方法 ---

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
                        if iv < 5.0: iv *= 100.0
                        min_dist, best_strike = dist, s_val
                        best_data = {"iv": iv, "mark": float(c.get("mark") or 0.0), 
                                     "delta": float(c.get("delta") or 0.0), 
                                     "theta": float(c.get("theta") or 0.0), 
                                     "gamma": float(c.get("gamma") or 0.0)}
            if best_data:
                term.append(TermPoint(exp=date_iso, dte=dte, strike=best_strike, **best_data))
        return price, term, chain