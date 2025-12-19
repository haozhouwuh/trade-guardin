from __future__ import annotations
import os
import sys
import pandas as pd
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
            print(f"\n❌ [CRITICAL ERROR] Tickers file NOT FOUND")
            sys.exit(1)
        df = pd.read_csv(self.tickers_path, header=None)
        return df[0].dropna().apply(lambda x: str(x).strip().upper()).tolist()

    def scanlist(self, strategy_name: str = "auto", days: int = 600, 
                 min_score: int = 60, max_risk: int = 70, detail: bool = False,
                 limit: int = None, **kwargs):
        
        start_time = time.time()
        
        # 1. 环境初始化
        try:
            vix_q = self.client.get_quote("$VIX")
            current_vix = vix_q.get("lastPrice", 0.0) 
        except: current_vix = 0.0
        
        tickers = self._get_universe()
        if limit: tickers = tickers[:limit]

        # 核心数据容器
        db_results_pack = []  
        all_rows_for_stats = [] 
        current_rows_for_next_batch = [] 
        strict_results = []  # 修正：定义蓝图暂存容器
        
        print("\n" + "=" * 115)
        print(f"🧠 TRADE GUARDIAN :: SCANLIST | VIX: {current_vix:.2f} | Depth: {days}d")
        print("-" * 115)
        # 补回 Base 栏目
        print(f"{'Sym':<6} {'Px':<9} {'ShortExp':<12} {'DTE':<4} {'ShortIV':<9} {'BaseIV':<9} {'Edge':<8} {'Score':<6} {'DNA':<12} {'Gate':<8}")
        print("-" * 115)

        for ticker in tickers:
            try:
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: continue
                
                strategy = self._load_strategy("long_gamma")
                row = strategy.evaluate(ctx)
                if not row: continue

                # A. 动能计算 (Δ15m)
                iv_diff = 0.0
                if self.last_batch_df is not None:
                    prev = self.last_batch_df[self.last_batch_df['symbol'] == row.symbol]
                    if not prev.empty:
                        iv_diff = row.short_iv - prev.iloc[0]['iv']
                
                dna_type = "QUIET"
                if iv_diff > 2.0: dna_type = "PULSE"
                elif iv_diff > 0.5: dna_type = "TREND"
                elif iv_diff < -1.0: dna_type = "CRUSH"

                # B. 获取裁决状态
                row.meta["delta_15m"] = iv_diff
                bp = self.plan(ctx, row)
                gate = self._get_gate_status(row, bp, dna_type) 
                
                # C. 数据装包
                db_results_pack.append((row, ctx, bp, gate)) 
                all_rows_for_stats.append(row)
                current_rows_for_next_batch.append({'symbol': row.symbol, 'iv': row.short_iv})
                
                # 记录符合条件的蓝图
                if gate != "FORBID":
                    strict_results.append((row, ctx, bp, gate, dna_type))

                # D. 表格渲染 (补回 Base 栏目并锁定对齐)
                dna_map = {"PULSE": "PULSE", "TREND": "TREND", "CRUSH": "CRUSH", "QUIET": "QUIET"}
                dna_render = dna_map.get(dna_type, "QUIET")
                g_color = Fore.GREEN if gate == "EXEC" else (Fore.YELLOW if gate == "WAIT" else Fore.RED)
                
                # 严格对齐格式化
                print(f"{row.symbol:<6} {row.price:<9.2f} {row.short_exp:<12} {row.short_dte:<4} "
                      f"{str(round(row.short_iv, 1))+'%':>9} {str(round(row.base_iv, 1))+'%':>9} "
                      f"{str(round(row.edge, 2))+'x':>8} {row.cal_score:>6} {dna_render:<12} {g_color}{gate:<8}{Style.RESET_ALL}")

            except Exception as e:
                # print(f"❌ {ticker} Error: {str(e)}") 
                continue

        # 2. 统计计算
        elapsed = round(time.time() - start_time, 2)
        avg_edge = sum(r.edge for r in all_rows_for_stats) / len(all_rows_for_stats) if all_rows_for_stats else 0.0
        cheap_pct = (len([r for r in all_rows_for_stats if r.edge > 0]) / len(all_rows_for_stats) * 100.0) if all_rows_for_stats else 0.0

        # 3. 数据持久化
        self.last_batch_df = pd.DataFrame(current_rows_for_next_batch)
        self.db.save_scan_session(strategy_name, current_vix, len(tickers), avg_edge, cheap_pct, elapsed, db_results_pack)

        # 4. 打印蓝图
        if detail and strict_results:
            print("\n" + "🚀 Actionable Blueprints")
            print("-" * 115)
            for row, ctx, bp, gate, dna in strict_results:
                self._print_enhanced_blueprint(bp, row, dna)
        
        print("-" * 115)
        print(f"💾 [DB] Persistent Success. Batch ID recorded.")

    def _get_gate_status(self, row: ScanRow, bp: Optional[Blueprint], dna_type: str) -> str:
        est_gamma = row.meta.get("est_gamma", 0.0)
        if not bp or bp.error or est_gamma >= 0.25: # 微调 Gamma 阈值
            return "FORBID"
        d15 = row.meta.get("delta_15m", 0.0)
        if dna_type in ["PULSE", "TREND"] and d15 >= 1.0:
            return "EXEC"
        return "WAIT"

    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, dna: str):
        print(f" {Fore.WHITE}{bp.symbol} DNA: {dna}{Style.RESET_ALL}")
        print(f"    Est.Debit: ${bp.est_debit:.2f} | Gamma: {row.meta.get('est_gamma', 0.0):.4f}")
        for leg in bp.legs:
            print(f"    {'+' if leg.action == 'BUY' else '-'}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        print(f"    {Fore.CYAN}{Style.BRIGHT}📋 EXIT TEMPLATE: {self._get_temp(dna)}{Style.RESET_ALL}")
        print(f"    {'='*80}\n")

    def _get_temp(self, dna):
        if dna == "PULSE": return "寿命 < 30m | Δ15m 转负即撤"
        if dna == "TREND": return "寿命 > 60m | 盯紧 VIX 趋势"
        if dna == "CRUSH": return "⚠️ 风险: IV 快速萎缩，建议回避"
        return "寿命: 待定 | 关注盘整突破 | 低成本潜伏"

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        atm = round(row.price, 1)
        legs = [OrderLeg(ctx.symbol, "BUY", 1, row.short_exp, atm, "CALL"), 
                OrderLeg(ctx.symbol, "BUY", 1, row.short_exp, atm, "PUT")]
        # 简单估算借记额 (Mid Price 估算)
        est_debit = 0.0
        if hasattr(ctx, 'raw_chain'):
            # 这里可以从 raw_chain 提取 mid 价，暂时给个 placeholder
            est_debit = 1.0 
        return Blueprint(ctx.symbol, "STRADDLE", legs, est_debit, "Gamma Plan")

    def _load_strategy(self, name: str):
        if name == "long_gamma": return LongGammaStrategy(self.cfg, self.policy)
        return None