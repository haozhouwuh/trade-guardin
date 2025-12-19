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
        [REAL MODE] 仅使用真实 API 数据。
        """
        try:
            # 1. 获取 HV 数据 (真实 API)
            hv_info = self.calculate_hv_percentile(symbol)
            
            # 容错：如果 HV 获取失败，不要直接崩，给个默认值但标记 Warning
            if getattr(hv_info, 'status', '') == "Error":
                print(f"  [Warn] HV Fetch failed for {symbol}: {getattr(hv_info, 'msg', 'Unknown')}")
                # Fallback: 使用默认 HV Rank 50，防止后续计算除零
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            # 2. 获取 Term Structure 和 Chain (真实 API)
            # 这会返回: 股价, TermPoints列表, 原始Chain字典
            # 这里的 days 参数决定了我们要抓多远的期权链来寻找 LEAPS
            try:
                price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            except Exception as e:
                print(f"  [Warn] Chain scan failed for {symbol}: {e}")
                return None

            if not term_points:
                print(f"  [Warn] No term points found for {symbol}")
                return None

            # 3. 分析期限结构 (Term Structure)
            term_points.sort(key=lambda x: x.dte)

            # 寻找 Short Term (~30 DTE, 范围 15-45)
            short_candidates = [p for p in term_points if 15 <= p.dte <= 50]
            if not short_candidates:
                # 如果没有理想的，就找最接近 30 的
                short_point = min(term_points, key=lambda x: abs(x.dte - 30))
            else:
                short_point = min(short_candidates, key=lambda x: abs(x.dte - 30))
            
            # 寻找 Base Term (~150 DTE)
            base_point = min(term_points, key=lambda x: abs(x.dte - 150))
            if base_point.dte < 60:
                base_point = term_points[-1]

            # 4. 确定 IV 和 Regime
            short_iv = short_point.iv
            base_iv = base_point.iv
            
            # 防止除零
            if base_iv == 0: base_iv = short_iv if short_iv > 0 else 1.0

            # 判断 Contango
            regime = "CONTANGO" if base_iv > short_iv else "BACKWARDATION"
            # 如果差异很小 (< 2个IV点)，视为 Normal
            if abs(base_iv - short_iv) < 2.0:
                regime = "NORMAL"

            # 5. 组装 IVData
            iv_data = IVData(
                rank=hv_info.hv_rank, # 暂时复用 HV Rank
                percentile=0.0,
                current_iv=short_iv,
                hv_rank=hv_info.hv_rank,
                current_hv=hv_info.current_hv
            )

            # 6. 组装 TSF
            tsf = {
                "regime": regime,
                "curvature": "FLAT",
                "short_exp": short_point.exp,
                "short_dte": short_point.dte,
                "short_iv": short_iv,
                "base_iv": base_iv
            }

            # 7. 组装 Metrics (Greeks)
            class Metrics:
                pass
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
            # traceback.print_exc()
            return None

    # --- 基础 API 方法 ---

    def _headers(self):
        token = fetch_schwab_token()
        if not token:
            raise ValueError("Token fetch failed (None returned)")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_quote(self, symbol: str) -> dict:
        encoded = quote(symbol, safe="")
        url = self.QUOTE_URL_TEMPLATE.format(symbols=encoded)
        try:
            resp = requests.get(url, headers=self._headers(), timeout=20)
            if resp.status_code != 200:
                print(f"  [API Fail] Quote {symbol}: {resp.status_code}")
                return {}
            return resp.json().get(symbol, {}).get("quote", {}) or {}
        except Exception as e:
            print(f"  [Net Fail] Quote {symbol}: {e}")
            raise e

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
            if df.empty: return HVInfo(status="Error", msg="Empty candle df")
            
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

            # HV Rank
            hv_rank = 0.0 if hv_high == hv_low else (current_hv - hv_low) / (hv_high - hv_low) * 100.0
            
            return HVInfo(
                status="Success",
                current_hv=current_hv,
                hv_rank=hv_rank,
                hv_low=hv_low,
                hv_high=hv_high
            )
        except Exception as e:
            return HVInfo(status="Error", msg=str(e))

    def _fetch_calls_chain(self, symbol: str, from_d: str, to_d: str, range_val: str = "NTM") -> dict:
        params = {
            "symbol": symbol,
            "contractType": "ALL",
            "includeUnderlyingQuote": "true",
            "strategy": "SINGLE",
            "range": range_val,
            "fromDate": from_d,
            "toDate": to_d,
        }
        try:
            resp = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            
            # 如果 ALL 失败，退回到 ATM
            if resp.status_code == 400 and range_val == "ALL":
                params["range"] = "ATM"
                retry = requests.get(self.OPTION_CHAIN_URL, headers=self._headers(), params=params, timeout=30)
                if retry.status_code == 200:
                    return retry.json()
            return {}
        except Exception as e:
            print(f"  [Net Fail] Chain {symbol}: {e}")
            raise e

    def scan_atm_term(self, symbol: str, days: int, contract_type: str = "CALL") -> tuple[float, list[TermPoint], dict]:
        # 1. Get Quote
        q = self.get_quote(symbol)
        price = q.get("lastPrice") or q.get("closePrice") or q.get("mark")
        if not price:
            raise RuntimeError(f"No price for {symbol}")
        price = float(price)

        # 2. Get Chain
        from_date = datetime.now().strftime("%Y-%m-%d")
        to_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        # 尝试获取 ALL (Call+Put)，如果没有则可能只返回 Call
        chain = self._fetch_calls_chain(symbol, from_date, to_date, range_val="ALL")

        call_map = chain.get("callExpDateMap") or {}
        # 有时候 Schwab API 返回 putExpDateMap 但没有 callExpDateMap (极少见)
        if not call_map and not chain.get("putExpDateMap"):
            raise RuntimeError(f"Empty chain for {symbol}")

        dates = sorted(list(call_map.keys()))
        term: list[TermPoint] = []

        for date_str in dates:
            parts = date_str.split(":")
            date_iso = parts[0]
            try:
                dte = int(parts[1])
            except:
                continue

            strikes_map = call_map[date_str]
            min_dist = 1e18
            best: dict | None = None
            best_strike = 0.0

            # 寻找 ATM 期权
            for s_str, contracts in strikes_map.items():
                try:
                    s_val = float(s_str)
                    dist = abs(s_val - price)
                    if dist < min_dist:
                        c = contracts[0]
                        iv = float(c.get("volatility", 0) or 0.0)
                        mark = float(c.get("mark") or c.get("closePrice") or 0.0)
                        
                        # [Fix] 如果 IV 是 0，可能没有成交，跳过
                        if iv > 0.0:
                            # [Fix] Schwab IV 修正：如果 < 5.0 且明显不是低波 (e.g. DTE很短)，可能是小数
                            # 但通常 Schwab 返回 25.5 代表 25.5%。
                            # 安全起见，我们假设如果 API 返回 > 2.0 就是百分比
                            # 如果 < 1.0 (e.g. 0.25)，那就是小数，需要 * 100
                            # 这里保留您之前的逻辑：如果 < 5.0，乘以 100
                            if iv < 5.0: iv *= 100.0
                            
                            min_dist = dist
                            best = {
                                "iv": iv, 
                                "mark": mark, 
                                "delta": float(c.get("delta") or 0.0), 
                                "theta": float(c.get("theta") or 0.0), 
                                "gamma": float(c.get("gamma") or 0.0)
                            }
                            best_strike = s_val
                except:
                    continue

            if best:
                term.append(
                    TermPoint(
                        exp=date_iso,
                        dte=dte,
                        strike=best_strike,
                        mark=best["mark"],
                        iv=best["iv"],
                        delta=best["delta"],
                        theta=best["theta"],
                        gamma=best["gamma"],
                    )
                )

        return price, term, chain