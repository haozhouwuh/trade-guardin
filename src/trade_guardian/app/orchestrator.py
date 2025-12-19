from __future__ import annotations
import os
import sys
import pandas as pd
import traceback
import time
from typing import List, Tuple, Optional, Any
from colorama import Fore, Style

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg
from trade_guardian.app.persistence import PersistenceManager

try:
    from trade_guardian.strategies.long_gamma import LongGammaStrategy
    from trade_guardian.strategies.diagonal import DiagonalStrategy
except ImportError:
    pass

class TradeGuardian:
    def __init__(self, client, cfg: dict, policy, strategy=None):
        self.client = client
        self.cfg = cfg
        self.policy = policy
        self.strategy = strategy 
        self.tickers_path = os.path.join("data", "tickers.csv")
        self.db = PersistenceManager()
        self.last_batch_df: Optional[pd.DataFrame] = None 

    def _get_universe(self) -> List[str]:
        if not os.path.exists(self.tickers_path):
            print(f"\n❌ [CRITICAL ERROR] Tickers file NOT FOUND at: {os.path.abspath(self.tickers_path)}")
            sys.exit(1)
        try:
            df = pd.read_csv(self.tickers_path, header=None)
            tickers = df[0].dropna().apply(lambda x: str(x).strip().upper()).tolist()
            unique_tickers = []
            for t in tickers:
                if t and t not in unique_tickers: unique_tickers.append(t)
            return unique_tickers
        except Exception as e:
            print(f"❌ [CRITICAL ERROR] Failed to parse {self.tickers_path}: {e}")
            sys.exit(1)

    def scanlist(self, strategy_name: str = "auto", days: int = 600, 
                 min_score: int = 60, max_risk: int = 70, detail: bool = False,
                 limit: int = None, top: int = None, **kwargs):
        
        try:
            vix_q = self.client.get_quote("$VIX")
            current_vix = vix_q.get("lastPrice", 0.0)
        except:
            current_vix = 0.0
        
        print("=" * 115)
        print(f"🧠 TRADE GUARDIAN :: SCANLIST (days={days}) | VIX: {current_vix:.2f}")
        print("=" * 115)

        tickers = self._get_universe()
        if limit and limit > 0: tickers = tickers[:limit]

        headers = f"{'Sym':<6} {'Px':<8} {'ShortExp':<12} {'DTE':<4} {'ShortIV':<8} {'BaseIV':<8} {'Edge':<8} {'HV%':<6} {'Score':<6} {'Risk':<5} {'Gate':<8} {'Tag'}"
        print(headers)
        print("-" * 115)

        strict_results: List[Tuple[ScanRow, Context, Optional[Blueprint], str]] = []
        current_rows = []
        
        for ticker in tickers:
            try:
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: continue
                
                strategies_to_run = []
                if self.strategy: strategies_to_run = [self.strategy]
                elif strategy_name == "auto":
                    strategies_to_run = [self._load_strategy("long_gamma"), self._load_strategy("diagonal")]
                else: strategies_to_run = [self._load_strategy(strategy_name)]

                best_row = None
                for strategy in [s for s in strategies_to_run if s is not None]:
                    row = strategy.evaluate(ctx)
                    if not best_row or row.cal_score > best_row.cal_score: best_row = row

                if best_row:
                    bp = self.plan(ctx, best_row)
                    gate_status = self._get_gate_status(best_row, bp) 
                    self._print_row(best_row, min_score, max_risk, gate_status)
                    current_rows.append({'symbol': best_row.symbol, 'price': best_row.price, 'iv': best_row.short_iv})
                    
                    if gate_status != "FORBID" and best_row.cal_score >= min_score and best_row.short_risk <= max_risk:
                        strict_results.append((best_row, ctx, bp, gate_status))
            except Exception:
                continue

        current_batch_df = pd.DataFrame(current_rows)
        if self.last_batch_df is not None and not current_batch_df.empty:
            self._check_fomo_alerts(current_batch_df, self.last_batch_df, current_vix)
        
        # 排序
        strict_results.sort(key=lambda x: ({"EXEC": 0, "WARN": 1, "REJECT": 2, "FORBID": 3}.get(x[3], 4), -x[0].edge))
        display_results = strict_results[:top] if top and top > 0 else strict_results

        if detail and display_results:
            print("\n🚀 Actionable Blueprints (Execution Plan)")
            print("-" * 115)
            for row, ctx, bp, gate in display_results:
                # 即使没有新信号，也要打印蓝图
                self._print_enhanced_blueprint(bp, row, current_vix)

        # 更新历史数据用于下一轮对比
        self.last_batch_df = current_batch_df

        print("-" * 115)
        count = max(1, len(strict_results))
        self.db.save_scan_session(
            strategy_name=strategy_name, vix=current_vix, universe_size=len(tickers),
            avg_edge=sum(abs(r[0].edge) for r in strict_results)/count,
            cheap_pct=(sum(1 for r in strict_results if r[0].edge > 0)/count)*100,
            elapsed=kwargs.get("elapsed", 0.0), results=strict_results 
        )

    def _get_gate_status(self, row: ScanRow, bp: Optional[Blueprint]) -> str:
        gamma = row.meta.get("est_gamma", 0.0)
        if gamma >= 0.20: return "FORBID" 
        if not bp: return "ERROR" 
        if bp.error: return "REJECT" 
        if "HIGH" in bp.note or "EXTREME" in bp.note: return "WARN"
        return "EXEC"

    def _check_fomo_alerts(self, now_df: pd.DataFrame, prev_df: pd.DataFrame, current_vix: float):
        comp = pd.merge(now_df, prev_df, on='symbol', suffixes=('_n', '_p'))
        for _, r in comp.iterrows():
            px_pct = (r['price_n'] - r['price_p']) / r['price_p']
            iv_diff_15m = r['iv_n'] - r['iv_p']
            iv_drift_1h = self.db.get_latest_drift_1h(r['symbol']) 

            if px_pct > 0.005 and iv_diff_15m > 1.0:
                print(Fore.RED + Style.BRIGHT + f"🚀 [FOMO] {r['symbol']}: Px +{px_pct:.2%} & IV +{iv_diff_15m:+.1f}%" + Style.RESET_ALL)
            if iv_drift_1h > 2.0 and iv_diff_15m < -0.5:
                print(Fore.YELLOW + Style.BRIGHT + f"🏹 [SLINGSHOT] {r['symbol']}: 1h Drift {iv_drift_1h:+.1f} | 15m Pullback {iv_diff_15m:+.1f}" + Style.RESET_ALL)

    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, vix: float):
        """全面增强版蓝图：实现 DNA 识别与天蓝色(Cyan)视觉优化"""
        if not bp: return
        iv_diff_15m = 0.0
        # 获取 15 分钟 IV 变化用于 DNA 判定
        if self.last_batch_df is not None:
            prev_row = self.last_batch_df[self.last_batch_df['symbol'] == bp.symbol]
            if not prev_row.empty:
                iv_diff_15m = row.short_iv - prev_row.iloc[0]['iv']

        # 1. 判定 DNA 类型、模板建议与显示颜色
        if iv_diff_15m > 2.0:
            dna, temp, color = "PULSE (脉冲🔥)", "寿命 < 30m | 核心指标: Δ15m 转负即撤 | 目标: 捕获瞬时波峰", Fore.CYAN
        elif iv_diff_15m > 0.5:
            dna, temp, color = "TREND (趋势🚀)", "寿命 > 60m | 核心指标: 盯紧 VIX 趋势 | 目标: 结构性波动扩张", Fore.GREEN
        elif iv_diff_15m < -1.0:
            dna, temp, color = "CRUSH (收缩❄️)", "⚠️ 风险: IV 正在快速萎缩 | 核心指标: 价格若无大动静应避开", Fore.YELLOW
        else:
            dna, temp, color = "QUIET (平静⏳)", "寿命: 待定 | 核心指标: 关注盘整区间突破 | 目标: 低成本潜伏", Fore.WHITE
        
        # 2. 打印头部与 DNA 标签
        print(f" {color}{bp.symbol} {bp.strategy:<10} DNA: {dna}{Style.RESET_ALL}")
        print(f"    Est.Debit: ${bp.est_debit:.2f} | Gamma: {row.meta.get('est_gamma', 0.0):.4f}")
        
        # 3. 打印期权腿明细
        for leg in bp.legs:
            print(f"    {'+' if leg.action == 'BUY' else '-'}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        
        # 4. 打印退出模板 (核心升级：使用天蓝色 Cyan 提高可读性)
        if bp.error:
            print(f"    {Fore.RED}❌ REJECTED: {bp.error}{Style.RESET_ALL}")
        else:
            # 修改此处为 Fore.CYAN + Style.BRIGHT 确保在黑色背景下清晰可见
            print(f"    {Fore.CYAN}{Style.BRIGHT}📋 EXIT TEMPLATE: {temp}{Style.RESET_ALL}")
            
        print(f"    {'='*80}\n")
        

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        stype = row.meta.get("strategy", "").lower()
        if stype == "diagonal": return self._plan_diagonal(ctx, row)
        if stype == "long_gamma" or "LG" in row.tag: return self._plan_straddle(ctx, row)
        return None

    def _plan_straddle(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        g = row.meta.get("est_gamma", 0.0)
        atm = round(row.price, 1)
        iv_dec = row.short_iv / 100.0 if row.short_iv > 2.0 else row.short_iv
        debit = 0.8 * row.price * iv_dec * ((max(1, row.short_dte)/365.0)**0.5)
        legs = [OrderLeg(ctx.symbol, "BUY", 1, row.short_exp, atm, "CALL"), 
                OrderLeg(ctx.symbol, "BUY", 1, row.short_exp, atm, "PUT")]
        return Blueprint(ctx.symbol, "STRADDLE", legs, debit, f"Gamma={g:.4f}", gamma_exposure=g)

    def _plan_diagonal(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        s_s, l_e, l_s = row.meta.get("short_strike"), row.meta.get("long_exp"), row.meta.get("long_strike")
        width = row.meta.get("spread_width", 0.0)
        if not (s_s and l_s and l_e): return None
        debit = max(0.0, row.price - l_s) + (width * 0.15)
        err = f"REJECTED: Debit > Width" if debit >= width else None
        legs = [OrderLeg(ctx.symbol, "BUY", 1, l_e, l_s, "CALL"), 
                OrderLeg(ctx.symbol, "SELL", 1, row.short_exp, s_s, "CALL")]
        return Blueprint(ctx.symbol, "DIAGONAL", legs, debit, f"Width=${width:.2f}", error=err)

    def _load_strategy(self, name: str):
        if name == "long_gamma": return LongGammaStrategy(self.cfg, self.policy)
        elif name == "diagonal": return DiagonalStrategy(self.cfg, self.policy)
        return None

    def _print_row(self, row: ScanRow, min_s: int, max_r: int, gate: str):
        risk_str = f"!{row.short_risk}!" if row.short_risk > max_r else f"{row.short_risk}"
        print(f"{row.symbol:<6} {row.price:<8.2f} {row.short_exp:<12} {row.short_dte:<4} "
              f"{row.short_iv:>6.1f}% {row.base_iv:>6.1f}% {row.edge:>7.2f}x    "
              f"{row.hv_rank:>4.0f}% {row.cal_score:>5} {risk_str:>5} {gate:<8} {row.tag}")