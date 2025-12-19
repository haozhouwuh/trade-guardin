from __future__ import annotations
import os
import sys
import pandas as pd
import traceback
from typing import List, Tuple, Optional, Any

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg

# 策略类导入
try:
    from trade_guardian.strategies.long_gamma import LongGammaStrategy
    from trade_guardian.strategies.diagonal import DiagonalStrategy
except ImportError:
    pass

class TradeGuardian:
    """
    Trade Guardian 主控程序
    严格执行: 从 CSV 读取名单，执行硬风控计划。
    """
    
    def __init__(self, client, cfg: dict, policy, strategy=None):
        self.client = client
        self.cfg = cfg
        self.policy = policy
        self.strategy = strategy 
        # 强制指定配置文件路径
        self.tickers_path = os.path.join("data", "tickers.csv")

    def _get_universe(self) -> List[str]:
        """
        [Strict Logic] 从 data/tickers.csv 读取名单。
        如果文件不存在，直接报错并退出程序。
        """
        if not os.path.exists(self.tickers_path):
            print(f"\n❌ [CRITICAL ERROR] Tickers file NOT FOUND at: {os.path.abspath(self.tickers_path)}")
            print("程序无法继续执行，请检查 data 目录。")
            sys.exit(1) # 强制退出

        try:
            # 读取 CSV，处理没有表头的情况
            df = pd.read_csv(self.tickers_path, header=None)
            # 取第一列，转为大写字符串，去除空值
            tickers = df[0].dropna().apply(lambda x: str(x).strip().upper()).tolist()
            # 过滤掉可能的重复项
            unique_tickers = []
            for t in tickers:
                if t and t not in unique_tickers:
                    unique_tickers.append(t)
            return unique_tickers
            
        except Exception as e:
            print(f"❌ [CRITICAL ERROR] Failed to parse {self.tickers_path}: {e}")
            sys.exit(1)

    def scanlist(self, strategy_name: str = "auto", days: int = 600, 
                 min_score: int = 60, max_risk: int = 70, detail: bool = False,
                 limit: int = None, top: int = None, **kwargs):
        
        print("=" * 105)
        print(f"🧠 TRADE GUARDIAN :: SCANLIST (days={days})")
        print("=" * 105)

        # 1. 获取名单 (来自 CSV)
        tickers = self._get_universe()
        
        if limit and limit > 0: 
            tickers = tickers[:limit]

        print(f"Universe Source: {self.tickers_path} ({len(tickers)} tickers)")
        print(f"Strict Filter:   score >= {min_score}, short_risk <= {max_risk}")
        if top: print(f"Top Focus:       Best {top} candidates (Gate > Edge > Risk)")
        print("-" * 105)
        
        # 表头对齐补丁已就位
        headers = f"{'Sym':<6} {'Px':<8} {'ShortExp':<12} {'DTE':<4} {'ShortIV':<8} {'BaseIV':<8} {'Edge':<8} {'HV%':<6} {'Score':<6} {'Risk':<4} {'Gate':<4} {'Tag'}"
        print(headers)
        print("-" * 105)

        strict_results: List[Tuple[ScanRow, Context, Optional[Blueprint], str]] = []
        
        # 2. 扫描循环
        for ticker in tickers:
            try:
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: continue

                strategies_to_run = []
                if self.strategy: 
                    strategies_to_run = [self.strategy]
                elif strategy_name == "auto":
                    strategies_to_run = [self._load_strategy("long_gamma"), self._load_strategy("diagonal")]
                else: 
                    strategies_to_run = [self._load_strategy(strategy_name)]

                strategies_to_run = [s for s in strategies_to_run if s is not None]
                if not strategies_to_run: continue

                best_row = None
                for strategy in strategies_to_run:
                    row = strategy.evaluate(ctx)
                    if not best_row or row.cal_score > best_row.cal_score:
                        best_row = row

                if best_row:
                    bp = self.plan(ctx, best_row)
                    gate_status = self._get_gate_status(bp)
                    
                    # 打印扫描行
                    self._print_row(best_row, min_score, max_risk, gate_status)
                    
                    # 只有符合过滤条件的才进入候选结果
                    if best_row.cal_score >= min_score and best_row.short_risk <= max_risk:
                        strict_results.append((best_row, ctx, bp, gate_status))
                
            except Exception as e:
                # 扫描单个出错不退出，继续下一个
                print(f"❌ Error scanning {ticker}: {e}")
                continue

        # 3. 蓝图排序与输出 (Top N)
        def sort_key(item):
            row, _, _, gate = item
            gate_clean = gate.strip()
            gate_prio = 3
            if gate_clean == "✅": gate_prio = 0
            elif gate_clean == "⚠️": gate_prio = 1
            elif gate_clean == "⛔": gate_prio = 2
            return (gate_prio, -row.edge, row.short_risk, -row.cal_score)

        strict_results.sort(key=sort_key)
        display_results = strict_results[:top] if top and top > 0 else strict_results

        if detail and display_results:
            print("\n🚀 Actionable Blueprints (Execution Plan)")
            print("-" * 105)
            for row, ctx, bp, gate in display_results:
                if bp: self._print_blueprint(bp)

        # 4. 统计诊断
        print("-" * 105)
        count = max(1, len(strict_results))
        avg_abs_edge = sum(abs(r[0].edge) for r in strict_results) / count
        pos_edge_count = sum(1 for r in strict_results if r[0].edge > 0)
        pos_edge_pct = (pos_edge_count / count) * 100
        avg_score = sum(r[0].cal_score for r in strict_results) / count
        
        print(f"🧾 Diagnostics (Universe)")
        print(f"   • Avg Score:     {avg_score:.1f}")
        print(f"   • Avg |Edge|:    {avg_abs_edge:.2f}x (Intensity)")
        print(f"   • Cheap Vol (%): {pos_edge_pct:.0f}% (Edge > 0)")
        print(f"\n[ Legend ]")
        print(f"   ✅ Executable   | ⚠️ High Risk (Review) | ⛔ Rejected (Policy)")

    # --- Blueprint Logic ---

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        stype = row.meta.get("strategy", "").lower()
        if stype == "diagonal": return self._plan_diagonal(ctx, row)
        if stype == "long_gamma" or "LG" in row.tag: return self._plan_straddle(ctx, row)
        return None

    def _get_gate_status(self, bp: Optional[Blueprint]) -> str:
        if not bp: return "❌" 
        if bp.error: return "⛔" 
        if "HIGH" in bp.note or "EXTREME" in bp.note:
             return "⚠️ " # 补空格对齐
        return "✅"

    def _plan_diagonal(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        short_strike = row.meta.get("short_strike")
        long_strike = row.meta.get("long_strike")
        long_exp = row.meta.get("long_exp")
        spread_width = row.meta.get("spread_width", 0.0)
        
        if not (short_strike and long_strike and long_exp): return None

        est_debit = max(0.0, row.price - long_strike) + (spread_width * 0.15)
        
        error_msg = None
        if est_debit >= spread_width:
             excess = est_debit - spread_width
             error_msg = (f"REJECTED: Debit > Width. Excess: ${excess:.2f}.\n"
                          f"       -> Try buying deeper ITM LEAPS or RAISING Short Strike.")

        safety_note = ""
        if est_debit > 0.90 * spread_width and not error_msg: 
            safety_note = " ⚠️ CAUTION: High Debit/Width Ratio"

        legs = [
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=long_exp, strike=long_strike, type="CALL"),
            OrderLeg(symbol=ctx.symbol, action="SELL", ratio=1, exp=row.short_exp, strike=short_strike, type="CALL")
        ]
        
        rationale = (f"PMCC Setup: Buy LEAPS / Sell Near-Term Call.\n"
                     f"   • Spread Width: ${spread_width:.2f}\n"
                     f"   • Est Debit:    ${est_debit:.2f} (Target < {spread_width:.2f}){safety_note}")

        if error_msg: rationale = f"Strategy Gate: Blocked by Risk Policy.\n   • {error_msg}"

        return Blueprint(symbol=ctx.symbol, strategy="DIAGONAL", legs=legs, 
                         est_debit=est_debit, note=rationale, error=error_msg)

    def _plan_straddle(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        est_gamma = row.meta.get("est_gamma", 0.0)
        atm_strike = round(row.price, 1)
        dte_years = max(1, row.short_dte) / 365.0
        vol_decimal = row.short_iv / 100.0 if row.short_iv > 2.0 else row.short_iv
        est_debit = 0.8 * row.price * vol_decimal * (dte_years ** 0.5)

        legs = [
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=row.short_exp, strike=atm_strike, type="CALL"),
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=row.short_exp, strike=atm_strike, type="PUT")
        ]

        risk_label = "NORMAL"
        if est_gamma >= 0.20: risk_label = "EXTREME ⛔"
        elif est_gamma >= 0.12: risk_label = "HIGH ⚠️  "
        elif est_gamma >= 0.08: risk_label = "ELEVATED 🔸"
        
        risk_alert = f" [{risk_label}]" if est_gamma >= 0.08 else ""
        note = (f"Long Gamma Play: Buy ATM Straddle.\n"
                f"   • Est Gamma (Total): {est_gamma:.4f}{risk_alert}\n"
                f"   • Breakeven move:    ±${est_debit:.2f}")

        return Blueprint(symbol=ctx.symbol, strategy="STRADDLE", legs=legs, 
                         est_debit=est_debit, note=note, gamma_exposure=est_gamma)

    # --- Helpers ---

    def _load_strategy(self, name: str):
        if name == "long_gamma": return LongGammaStrategy(self.cfg, self.policy)
        elif name == "diagonal": return DiagonalStrategy(self.cfg, self.policy)
        return None

    def _print_row(self, row: ScanRow, min_score: int, max_risk: int, gate_status: str):
        risk_str = f"!{row.short_risk}!" if row.short_risk > max_risk else f"{row.short_risk}"
        s_iv_str, b_iv_str, hv_str = f"{row.short_iv:.1f}%", f"{row.base_iv:.1f}%", f"{row.hv_rank:.0f}%"
        # 物理空格对齐补丁 (gate_status + 2空格)
        print(f"{row.symbol:<6} {row.price:<8.2f} {row.short_exp:<12} {row.short_dte:<4} "
              f"{s_iv_str:<8} {b_iv_str:<8} {row.edge:+.2f}x   "
              f"{hv_str:<6} {row.cal_score:<6} {risk_str:<4} {gate_status}  {row.tag}")
    
    def _print_blueprint(self, bp: Blueprint):
        print(f" {bp.symbol} {bp.strategy:<10} Est.Debit: ${bp.est_debit:.2f}")
        if bp.legs:
            for leg in bp.legs:
                action_sign = "+" if leg.action == "BUY" else "-"
                print(f"    {action_sign}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        print(f"    {'='*30}") 
        if bp.error:
            print(f"    ⛔ {bp.error.splitlines()[0]}")
            lines = bp.note.split('\n')
            for line in lines:
                if "Try" in line or "Reason" in line: print(f"    {line}")
        else:
            for line in bp.note.split('\n'): print(f"    {line}")
        print("")