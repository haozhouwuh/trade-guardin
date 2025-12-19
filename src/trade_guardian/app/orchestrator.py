from __future__ import annotations
import traceback
from typing import List, Tuple, Optional, Any

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg

# 尝试导入策略类
try:
    from trade_guardian.strategies.long_gamma import LongGammaStrategy
    from trade_guardian.strategies.diagonal import DiagonalStrategy
except ImportError:
    pass

class TradeGuardian:
    """
    Trade Guardian 主控程序
    负责协调 Data Client, Strategy Scanner 和 Blueprint Generation
    """
    
    def __init__(self, client, cfg: dict, policy, strategy=None):
        self.client = client
        self.cfg = cfg
        self.policy = policy
        self.strategy = strategy 

    def scanlist(self, strategy_name: str = "auto", days: int = 600, 
                 min_score: int = 60, max_risk: int = 70, detail: bool = False,
                 limit: int = None, **kwargs):
        """
        CLI 命令: 扫描列表并生成交易蓝图
        """
        print("=" * 95)
        print(f"🧠 TRADE GUARDIAN :: SCANLIST (days={days})")
        print("=" * 95)

        # 1. 获取观察列表
        tickers = self._get_universe()
        
        if limit and limit > 0:
            tickers = tickers[:limit]

        print(f"Universe size: {len(tickers)} | Strategy: {strategy_name.upper()}")
        print(f"Strict Filter: score >= {min_score}, short_risk <= {max_risk}")
        print("-" * 95)
        
        headers = f"{'Sym':<6} {'Px':<8} {'ShortExp':<12} {'DTE':<4} {'ShortIV':<8} {'BaseIV':<8} {'Edge':<8} {'HV%':<6} {'Score':<6} {'Risk':<4} {'Tag'}"
        print(headers)
        print("-" * 95)

        strict_results: List[Tuple[ScanRow, Context]] = []
        
        # 2. 扫描循环
        for ticker in tickers:
            try:
                # 构建上下文
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: 
                    # print(f"Skipping {ticker}: No Context built")
                    continue

                # 确定策略
                strategies_to_run = []
                
                if self.strategy:
                    strategies_to_run = [self.strategy]
                elif strategy_name == "auto":
                    strategies_to_run = [
                        self._load_strategy("long_gamma"),
                        self._load_strategy("diagonal")
                    ]
                else:
                    strategies_to_run = [self._load_strategy(strategy_name)]

                strategies_to_run = [s for s in strategies_to_run if s is not None]

                if not strategies_to_run:
                    continue

                best_row = None
                
                for strategy in strategies_to_run:
                    row = strategy.evaluate(ctx)
                    
                    if not best_row or row.cal_score > best_row.cal_score:
                        best_row = row

                if best_row:
                    self._print_row(best_row, min_score, max_risk)
                    
                    if best_row.cal_score >= min_score and best_row.short_risk <= max_risk:
                        strict_results.append((best_row, ctx))
                
            except Exception as e:
                print(f"❌ Error scanning {ticker}: {e}")
                # traceback.print_exc()
                continue

        # 5. 打印 Actionable Blueprints
        if detail and strict_results:
            print("\n🚀 Actionable Blueprints (Execution Plan)")
            print("-" * 95)
            
            for row, ctx in strict_results:
                bp = self.plan(ctx, row)
                if bp:
                    self._print_blueprint(bp)

        print("-" * 95)
        count = max(1, len(strict_results))
        avg_score = sum(r[0].cal_score for r in strict_results) / count
        avg_edge = sum(r[0].edge for r in strict_results) / count
        print(f"🧾 Diagnostics\n   • Avg Score: {avg_score:.1f} | Avg Edge: {avg_edge:.2f}x")

    # --- Blueprint Logic ---

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        strategy_type = row.meta.get("strategy", "").lower()
        
        if strategy_type == "diagonal":
            return self._plan_diagonal(ctx, row)
            
        if strategy_type == "long_gamma" or "LG" in row.tag:
            return self._plan_straddle(ctx, row)
            
        return None

    def _plan_diagonal(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        short_strike = row.meta.get("short_strike")
        long_strike = row.meta.get("long_strike")
        long_exp = row.meta.get("long_exp")
        spread_width = row.meta.get("spread_width", 0.0)
        
        if not (short_strike and long_strike and long_exp):
            return None

        est_debit = max(0.0, row.price - long_strike) + (spread_width * 0.15)
        
        safety_note = ""
        max_loss = spread_width - est_debit
        if max_loss < 0:
            safety_note = " ⚠️ WARNING: Est Debit > Width (Locked Loss Risk!)"

        legs = [
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=long_exp, strike=long_strike, type="CALL"),
            OrderLeg(symbol=ctx.symbol, action="SELL", ratio=1, exp=row.short_exp, strike=short_strike, type="CALL")
        ]
        
        rationale = (
            f"PMCC Setup: Buy LEAPS / Sell Near-Term Call.\n"
            f"   • Spread Width: ${spread_width:.2f}\n"
            f"   • Est Debit:    ${est_debit:.2f} (Target < {spread_width:.2f})\n"
            f"   • Edge:         {row.edge:.2f} (Short IV > Long IV){safety_note}"
        )

        return Blueprint(symbol=ctx.symbol, strategy="DIAGONAL", legs=legs, est_debit=est_debit, note=rationale)

    def _plan_straddle(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        est_gamma = row.meta.get("est_gamma", 0.0)
        target_exp = row.short_exp
        
        atm_strike = round(row.price, 1)

        dte_years = max(1, row.short_dte) / 365.0
        vol_decimal = row.short_iv / 100.0 if row.short_iv > 2.0 else row.short_iv
        est_debit = 0.8 * row.price * vol_decimal * (dte_years ** 0.5)

        legs = [
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=target_exp, strike=atm_strike, type="CALL"),
            OrderLeg(symbol=ctx.symbol, action="BUY", ratio=1, exp=target_exp, strike=atm_strike, type="PUT")
        ]

        risk_alert = ""
        if est_gamma > 0.15:
            risk_alert = f" ⚠️ HIGH GAMMA RISK ({est_gamma:.4f})"

        note = (
            f"Long Gamma Play: Buy ATM Straddle.\n"
            f"   • Est Gamma:      {est_gamma:.4f}{risk_alert}\n"
            f"   • Breakeven move: ±${est_debit:.2f}"
        )

        return Blueprint(
            symbol=ctx.symbol, 
            strategy="STRADDLE", 
            legs=legs, 
            est_debit=est_debit, 
            note=note, 
            gamma_exposure=est_gamma
        )

    # --- Helpers ---

    def _get_universe(self) -> List[str]:
        return [
            "AMD", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL",
            "SPY", "QQQ", "IWM", "TQQQ", "SQQQ", "SOXL", "TSLL",
            "COIN", "MSTR", "ONDS", "SMCI", "BABA", "LLY"
        ]

    def _load_strategy(self, name: str):
        if name == "long_gamma":
            return LongGammaStrategy(self.cfg, self.policy)
        elif name == "diagonal":
            return DiagonalStrategy(self.cfg, self.policy)
        return None

    def _print_row(self, row: ScanRow, min_score: int, max_risk: int):
        edge_str = f"{row.edge:+.2f}x"
        
        risk_str = f"{row.short_risk}"
        if row.short_risk > max_risk:
            risk_str = f"!{row.short_risk}!" 
        
        # 修复百分比格式显示，移除多余空格
        s_iv_str = f"{row.short_iv:.1f}%"
        b_iv_str = f"{row.base_iv:.1f}%"
        hv_str = f"{row.hv_rank:.0f}%"

        print(f"{row.symbol:<6} {row.price:<8.2f} {row.short_exp:<12} {row.short_dte:<4} "
              f"{s_iv_str:<8} {b_iv_str:<8} {edge_str:<8} "
              f"{hv_str:<6} {row.cal_score:<6} {risk_str:<4} {row.tag}")
    
    def _print_blueprint(self, bp: Blueprint):
        print(f" {bp.symbol} {bp.strategy:<10} Est.Debit: ${bp.est_debit:.2f}")
        
        # [Critical Fix] 显式打印每一条腿 (Actionable Details)
        if bp.legs:
            for leg in bp.legs:
                action_sign = "+" if leg.action == "BUY" else "-"
                # 格式: +1 2026-01-16 202.5 CALL
                print(f"    {action_sign}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        
        print(f"    {'='*30}") # 分隔线
        
        # 打印 Note
        lines = bp.note.split('\n')
        for line in lines:
            print(f"    {line}")
        print("")