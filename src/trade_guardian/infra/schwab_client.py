from __future__ import annotations

import requests
import numpy as np
import pandas as pd
import traceback
from datetime import datetime, timedelta, date
from urllib.parse import quote
from typing import Optional, Any

from trade_guardian.domain.models import Context, IVData, HVInfo, TermPoint
from trade_guardian.infra.schwab_token_manager import fetch_schwab_token

# --- Helper Functions (定义在类外部) ---
def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()

def is_third_friday(d: date) -> bool:
    """判断是否为标准月度期权 (第三个周五)"""
    # Friday is 4, range 15-21 ensures it's the 3rd Friday
    return d.weekday() == 4 and 15 <= d.day <= 21

def get_series_kind(exp_str: str) -> str:
    """根据到期日判断合约类型：MONTHLY, WEEKLY, DAILY"""
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
                # print(f"  [Warn] Chain scan failed for {symbol}: {e}")
                return None

            if not term_points:
                return None

            # 3. 分析期限结构 (Term Structure)
            term_points.sort(key=lambda x: x.dte)

            # --- [Step A: 寻找 Short Leg (目标 1-10 DTE)] ---
            short_candidates = [p for p in term_points if 1 <= p.dte <= 10]
            if short_candidates:
                # 避开 0DTE，选最近的
                short_point = min(short_candidates, key=lambda x: x.dte if x.dte > 0 else 999)
            else:
                short_point = term_points[0]
            
            # 提取 Short 的特征，供后续匹配
            short_kind = get_series_kind(short_point.exp)
            short_weekday = _to_date(short_point.exp).weekday()

            # --- [Step B: 寻找 Micro Base (短期节奏, 强制 > Short)] ---
            # 目标：寻找比 Short 更远的下一阶合约 (Next Step)
            # 修正核心：p.dte > short_point.dte
            micro_target = 10
            micro_pool = [p for p in term_points if short_point.dte < p.dte <= 21]
            
            micro_point = None
            if not micro_pool:
                # 极罕见情况：没有比 Short 更远的合约了，退化为 Short (Edge=0)
                micro_point = short_point
            else:
                # 1. 优先找同 Weekday (日期对日期)
                micro_best = [p for p in micro_pool if _to_date(p.exp).weekday() == short_weekday]
                
                if not micro_best:
                    # 2. 退回同 Kind (比如都是 Weekly)
                    micro_best = [p for p in micro_pool if get_series_kind(p.exp) == short_kind]
                
                if not micro_best:
                     # 3. 兜底 (只要比 Short 远就行)
                     micro_best = micro_pool

                # 在符合条件的池子里，找最接近 micro_target (10) 的
                micro_point = min(micro_best, key=lambda x: abs(x.dte - micro_target))

            # --- [Step C: 寻找 Month Base (结构锚点, 目标 25-50 DTE)] ---
            month_target = 30
            month_pool = [p for p in term_points if 25 <= p.dte <= 50]
            
            month_point = None
            if not month_pool:
                # 兜底全场找最接近 30 的
                month_point = min(term_points, key=lambda x: abs(x.dte - month_target))
            else:
                # 1. 优先找同 Kind (主要是 Monthly 对 Monthly)
                month_best = [p for p in month_pool if get_series_kind(p.exp) == short_kind]
                
                if not month_best:
                    # 2. 兜底全场
                    month_best = month_pool
                
                month_point = min(month_best, key=lambda x: abs(x.dte - month_target))

            # ----------------------------------------

            # 4. 数据计算与组装
            short_iv = short_point.iv
            micro_iv = micro_point.iv
            month_iv = month_point.iv
            
            # 防止除零
            if micro_iv == 0: micro_iv = short_iv if short_iv > 0 else 1.0
            if month_iv == 0: month_iv = short_iv if short_iv > 0 else 1.0

            # 5. 组装 TSF：包含双基准全量信息
            tsf = {
                "regime": "NORMAL", # 占位
                "curvature": "FLAT",
                
                # Short Leg Info
                "short_exp": short_point.exp,
                "short_dte": short_point.dte,
                "short_iv": short_iv,
                "short_kind": short_kind,

                # Micro Base Info
                "micro_exp": micro_point.exp,
                "micro_dte": micro_point.dte,
                "micro_iv": micro_iv,
                
                # Month Base Info
                "month_exp": month_point.exp,
                "month_dte": month_point.dte,
                "month_iv": month_iv,
                
                # Pre-calculated Edges (双 Edge)
                "edge_micro": (micro_iv - short_iv) / short_iv if short_iv > 0 else 0.0,
                "edge_month": (month_iv - short_iv) / short_iv if short_iv > 0 else 0.0
            }

            # 6. 组装 Greeks & Data Assembly
            iv_data = IVData(
                rank=hv_info.hv_rank, 
                percentile=0.0, 
                current_iv=short_iv, 
                hv_rank=hv_info.hv_rank, 
                current_hv=hv_info.current_hv
            )
            
            class Metrics: pass
            metrics = Metrics()
            metrics.gamma = short_point.gamma
            metrics.delta = short_point.delta
            metrics.theta = short_point.theta

            # 7. 返回 Context
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