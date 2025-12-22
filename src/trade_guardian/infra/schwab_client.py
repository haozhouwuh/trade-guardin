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

# --- Helper Functions (物理特征辅助) ---
def _to_date(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()

def is_third_friday(d: date) -> bool:
    """判断是否为标准月度期权 (第三个周五)"""
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
        [Tactical Refactor] Curve-Driven Context Builder
        优化目标：在扫描全量地形的基础上，引入战术偏好 (Tactical Bias)
          1. Micro: 优先捕捉单位时间内的 IV 爆发力 (Front Momentum)
          2. Month: 在平稳区基础上，惩罚过远的 DTE，锚定 35DTE 战术区
        """
        try:
            # 1. 获取 HV 数据
            hv_info = self.calculate_hv_percentile(symbol)
            if getattr(hv_info, 'status', '') == "Error":
                hv_info = HVInfo(current_hv=0.0, hv_rank=50.0)

            # 2. 获取全量期权链
            try:
                price, term_points, raw_chain = self.scan_atm_term(symbol, days)
            except Exception as e:
                return None

            if not term_points or len(term_points) < 3:
                return None

            # 3. 排序
            term_points.sort(key=lambda x: x.dte)

            # =================================================================
            # 算法 A: 确定 Short Leg (执行腿)
            # 逻辑：1-10 DTE 中最近的非 0DTE
            # =================================================================
            short_candidates = [p for p in term_points if 1 <= p.dte <= 10]
            if short_candidates:
                short_point = min(short_candidates, key=lambda x: x.dte)
            else:
                short_point = term_points[0]
            
            short_kind = get_series_kind(short_point.exp)
            short_iv_base = short_point.iv if short_point.iv > 0 else 1.0

            # =================================================================
            # 算法 B: 寻找 Micro Anchor (引入前端爆发力评分)
            # 逻辑：在 1-15 DTE 中，优先找局部波峰；若无，找单位时间 IV 增幅最大的点
            # =================================================================
            micro_pool = [p for p in term_points if 1 <= p.dte <= 15]
            micro_point = None

            if len(micro_pool) >= 2:
                # 1. 优先：寻找局部极大值 (Local Maxima) -> 显性挤压
                local_maxima = []
                for i in range(1, len(micro_pool) - 1):
                    if micro_pool[i].iv > micro_pool[i-1].iv and micro_pool[i].iv > micro_pool[i+1].iv:
                        local_maxima.append(micro_pool[i])
                
                if local_maxima:
                    micro_point = max(local_maxima, key=lambda x: x.iv)
                else:
                    # 2. 兜底：无局部峰值时，寻找 "单位时间爆发力" 最强的点
                    # 公式：Score = (IV - ShortIV) / sqrt(DTE)
                    # 避免无脑选最远端，而是选曲线最陡峭的那一段
                    def _momentum_score(p):
                        d_eff = max(1, p.dte)
                        return (p.iv - short_iv_base) / np.sqrt(d_eff)
                    
                    micro_point = max(micro_pool, key=_momentum_score)
            
            # Fallback
            if not micro_point or micro_point.dte <= short_point.dte:
                candidates = [p for p in term_points if p.dte > short_point.dte and p.dte <= 21]
                if candidates:
                    micro_point = min(candidates, key=lambda x: abs(x.dte - 10))
                else:
                    micro_point = short_point

            # =================================================================
            # 算法 C: 寻找 Month Anchor (战术窗口修正)
            # 逻辑：优先在 25-45 DTE (战术区) 寻找锚点；若无数据，再扩至 25-90 DTE
            # =================================================================
            month_pool = [p for p in term_points if 25 <= p.dte <= 45]
            if len(month_pool) < 3:
                month_pool = [p for p in term_points if 25 <= p.dte <= 90]
            
            month_point = None
            
            # 使用简单的距离+平稳度打分 (保持原逻辑，但池子变了)
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
                
                # 在优胜组里优先选 Monthly
                best_monthly = next((p for sc, sd, p in top_5 if get_series_kind(p.exp) == "MONTHLY"), None)
                month_point = best_monthly if best_monthly else top_5[0][2]
            else:
                # Fallback
                fallback_candidates = [p for p in term_points if p.dte >= 20]
                if fallback_candidates:
                    month_point = min(fallback_candidates, key=lambda x: abs(x.dte - 30))
                else:
                    month_point = term_points[-1]


            # =================================================================
            # 4. 数据组装与 Edge 稳健计算 (Stabilizer V3)
            # =================================================================
            short_iv = short_point.iv
            micro_iv = micro_point.iv
            month_iv = month_point.iv

            if short_iv <= 0: short_iv = 1.0
            if month_iv <= 0: month_iv = 1.0

            # 结构判定
            is_squeeze = True if (micro_iv > month_iv * 1.05) else False
            
            regime = "FLAT"
            if short_iv > month_iv * 1.03: regime = "BACKWARDATION"
            elif month_iv > short_iv * 1.03: regime = "CONTANGO"

            curvature = "NORMAL"
            if micro_iv > short_iv * 1.10: curvature = "SPIKY_FRONT"

            # [Edge Stabilizer V3]
            # 1. Short Base (Median): 这里的 Short IV 容易受单日事件影响出现毛刺。
            #    使用 1-10 DTE 区间的中位数作为更稳健的"短期基准"。
            short_band = [p.iv for p in term_points if 1 <= p.dte <= 10 and p.iv > 0]
            short_base = np.median(short_band) if short_band else short_iv

            # 2. Mixed Denominator: 防止分母过小，同时也防止低波环境下 Edge 被过度压缩。
            #    使用 (Base + Far) / 2 作为分母，并保留 12.0 的硬地板。
            IV_FLOOR = 12.0
            denom_micro = max(0.5 * (short_base + micro_iv), IV_FLOOR)
            denom_month = max(0.5 * (short_base + month_iv), IV_FLOOR)
            
            edge_micro_raw = (micro_iv - short_base) / denom_micro
            edge_month_raw = (month_iv - short_base) / denom_month
            
            # 3. Extended Soft Decay: 将衰减范围扩至 6 DTE (周权覆盖区)
            #    系数从 0.7 起步 (sqrt(1/6)=0.4太狠了，我们设死底线)
            DECAY_CUTOFF = 6.0
            stabilizer = 1.0
            
            if short_point.dte < DECAY_CUTOFF:
                # 平滑因子
                raw_decay = np.sqrt(max(1, short_point.dte) / DECAY_CUTOFF)
                # 限制范围 [0.7, 1.0]，避免对 1-2 DTE 惩罚过重导致完全不可交易
                stabilizer = max(0.7, min(1.0, raw_decay))
                
                edge_micro_raw *= stabilizer
                edge_month_raw *= stabilizer

            # [DEBUG] 打印调试信息，验证数值是否合理 (正式版可注释)
            # print(f"[DBG] {symbol:<5} DTE:{short_point.dte} Base:{short_base:.1f}% M_IV:{month_iv:.1f}% Stab:{stabilizer:.2f} Edge:{edge_month_raw:.2f}")

            tsf = {
                "regime": regime,
                "curvature": curvature,
                "is_squeeze": is_squeeze,

                # Anchor Points
                "short_exp": short_point.exp, "short_dte": short_point.dte, "short_iv": short_point.iv, "short_kind": short_kind,
                "micro_exp": micro_point.exp, "micro_dte": micro_point.dte, "micro_iv": micro_point.iv,
                "month_exp": month_point.exp, "month_dte": month_point.dte, "month_iv": month_point.iv,

                # Stabilized Edges
                "edge_micro": edge_micro_raw,
                "edge_month": edge_month_raw,
            }
            
            # 5. 返回 Context
            iv_data = IVData(
                rank=hv_info.hv_rank, percentile=0.0, current_iv=short_iv, 
                hv_rank=hv_info.hv_rank, current_hv=hv_info.current_hv
            )
            
            class Metrics: pass
            metrics = Metrics()
            metrics.gamma = short_point.gamma
            metrics.delta = short_point.delta
            metrics.theta = short_point.theta

            # [FIX] P0-1: 这里增加了 term=term_points
            return Context(
                symbol=symbol, 
                price=price, 
                iv=iv_data, 
                hv=iv_data, 
                tsf=tsf, 
                raw_chain=raw_chain, 
                metrics=metrics,
                term=term_points 
            )
        
        except Exception as e:
            return None

    # --- 基础 API 方法保持不变 ---
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
        """一次性获取未来 N 天的全量期权链"""
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
                        best_data = {
                            "iv": iv, "mark": float(c.get("mark") or 0.0), 
                            "delta": float(c.get("delta") or 0.0), 
                            "theta": float(c.get("theta") or 0.0), 
                            "gamma": float(c.get("gamma") or 0.0)
                        }
            if best_data:
                term.append(TermPoint(exp=date_iso, dte=dte, strike=best_strike, **best_data))
        
        return price, term, chain