from __future__ import annotations
import os
import sys
import time
import pandas as pd
from typing import List, Tuple, Optional, Any
from colorama import Fore, Style

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg
from trade_guardian.app.persistence import PersistenceManager
from trade_guardian.strategies.blueprint import build_straddle_blueprint 
from trade_guardian.infra.rate_limit import RateLimiter

# [FIX] 移除硬编码，仅保留默认常量作为 Config 不存在时的兜底
DEFAULT_GAMMA_SOFT = 0.24 
DEFAULT_GAMMA_HARD = 0.32 
LEV_ETFS = ["TQQQ", "SQQQ", "SOXL", "SOXS", "TSLL", "TSLS", "NVDL", "LABU", "UVXY"]

class TradeGuardian:
    def __init__(self, client, cfg: dict, policy, strategy=None):
        self.client = client
        self.cfg = cfg
        self.policy = policy
        self.strategy = strategy 
        
        self.tickers_path = cfg.get("paths", {}).get("tickers_csv", "data/tickers.csv")
        throttle = float(cfg.get("scan", {}).get("throttle_sec", 0.5))
        self.limiter = RateLimiter(throttle)
        
        self.db = PersistenceManager()
        self.last_batch_df: Optional[pd.DataFrame] = None 
        
        # [FIX] 读取配置
        self.micro_min = float(cfg.get("rules", {}).get("diag_micro_min", 0.08))
        self.month_min = float(cfg.get("rules", {}).get("diag_month_min", 0.15))
        self.gamma_soft = float(cfg.get("rules", {}).get("gamma_soft_cap", DEFAULT_GAMMA_SOFT))
        self.gamma_hard = float(cfg.get("rules", {}).get("gamma_hard_cap", DEFAULT_GAMMA_HARD))

    def _get_universe(self) -> List[str]:
        if not os.path.exists(self.tickers_path):
            fallback = os.path.join("data", "tickers.csv")
            if os.path.exists(fallback):
                self.tickers_path = fallback
            else:
                print(f"\n❌ [CRITICAL ERROR] Tickers file NOT FOUND at {self.tickers_path}")
                sys.exit(1)
        df = pd.read_csv(self.tickers_path, header=None)
        return df[0].dropna().apply(lambda x: str(x).strip().upper()).tolist()

    def scanlist(self, strategy_name: str = "auto", days: int = 600, 
                 min_score: int = 60, max_risk: int = 70, detail: bool = False,
                 limit: int = None, **kwargs):
        
        start_ts = time.time()

        try:
            vix_q = self.client.get_quote("$VIX")
            current_vix = vix_q.get("lastPrice", 0.0) 
        except: current_vix = 0.0
        
        tickers = self._get_universe()
        if limit: tickers = tickers[:limit]

        db_results_pack = []  
        strict_results = [] 
        current_rows_for_next_batch = [] 
        
        FMT = "{sym:<5} {px:<7} {sexp:<11} {sdte:<3} {siv:>6} | {mexp:<11} {mdte:<3} {miv:>6} {em:>5} | {kexp:<11} {kdte:<3} {kiv:>6} {ek:>5} | {sc:>4} {shp:<8} {gate:<6}   {tag:<8}"
        HEADER = FMT.format(
            sym="Sym", px="Px", sexp="ShortExp", sdte="D", siv="S_IV",
            mexp="MicroExp", mdte="D", miv="M_IV", em="EdgM",
            kexp="MonthExp", kdte="D", kiv="K_IV", ek="EdgK",
            sc="Scr", shp="Shape", gate="Gate", tag="Tag"
        )
        WIDTH = len(HEADER)

        print("\n" + "=" * WIDTH)
        print(f"🧠 TRADE GUARDIAN :: V4.5 FINAL | VIX: {current_vix:.2f} | Strategy: {strategy_name}")
        print("-" * WIDTH)
        print(HEADER)
        print("-" * WIDTH)

        for ticker in tickers:
            self.limiter.sleep()

            try:
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: 
                    print(f"{Fore.RED}⚠️  SKIP {ticker:<5} | Reason: No Context{Style.RESET_ALL}")
                    continue
                
                current_strategy = self.strategy if self.strategy else self._load_strategy(strategy_name)
                row = current_strategy.evaluate(ctx)
                if not row: 
                    print(f"{Fore.YELLOW}⚠️  SKIP {ticker:<5} | Reason: Strategy Eval None{Style.RESET_ALL}")
                    continue

                # [FIX] 动能计算防污染 & DataFrame 安全访问
                iv_diff = 0.0
                if self.last_batch_df is not None and not self.last_batch_df.empty:
                    # 检查列是否存在
                    if 'short_exp' in self.last_batch_df.columns:
                        prev = self.last_batch_df[
                            (self.last_batch_df['symbol'] == row.symbol) & 
                            (self.last_batch_df['short_exp'] == row.short_exp)
                        ]
                        if not prev.empty:
                            iv_diff = row.short_iv - prev.iloc[0]['iv']
                
                mom_type = "QUIET"
                if iv_diff > 2.0: mom_type = "PULSE"
                elif iv_diff > 0.5: mom_type = "TREND"
                elif iv_diff < -1.0: mom_type = "CRUSH"
                
                row.meta["delta_15m"] = iv_diff
                row.meta["momentum"] = mom_type

                tsf = ctx.tsf or {}
                regime = str(tsf.get("regime", "FLAT"))
                is_squeeze = bool(tsf.get("is_squeeze", False))
                em = float(row.meta.get("edge_micro", 0) or 0)
                ek = float(row.meta.get("edge_month", 0) or 0)
                
                shape = "FLAT"
                if regime == "BACKWARDATION": shape = "BACKWARD"
                elif ek >= 0.20 and em < 0.08: shape = "FFBS"
                elif is_squeeze or em >= 0.12: shape = "SPIKE"
                elif ek >= 0.20: shape = "STEEP"
                elif 0.15 <= ek < 0.20: shape = "MILD"
                else: shape = "FLAT"
                row.meta["shape"] = shape
                
                bp = getattr(row, 'blueprint', None)
                if not bp: bp = self.plan(ctx, row) 
                
                gate, reason = self._get_gate_status(row, bp, mom_type)
                
                # bp.error 只认结构性错误；Gate Reason 写入 Note 和 Meta
                if gate != "EXEC" and gate != "LIMIT":
                    row.meta["gate_reason"] = reason 
                    if bp and not bp.note: bp.note = reason

                db_results_pack.append((row, ctx, bp, gate)) 
                
                # [FIX] 保存 short_exp 供下轮使用
                current_rows_for_next_batch.append({
                    'symbol': row.symbol, 
                    'iv': row.short_iv,
                    'short_exp': row.short_exp
                })
                
                if gate != "FORBID":
                    strict_results.append((row, ctx, bp, gate, mom_type, reason))

                if gate == "EXEC": g_color = Fore.GREEN
                elif gate == "LIMIT": g_color = Fore.CYAN
                elif gate == "FORBID": g_color = Fore.RED
                else: g_color = Fore.YELLOW
                
                gate_display = f"{g_color}{gate:<6}{Style.RESET_ALL}"
                tag_str = str(row.tag) if row.tag else ""

                print(FMT.format(
                    sym=row.symbol,
                    px=f"{row.price:.1f}",
                    sexp=row.short_exp, sdte=row.short_dte, siv=f"{int(row.short_iv)}%",
                    mexp=str(row.meta.get("micro_exp", "N/A")), mdte=str(row.meta.get("micro_dte", 0)), miv=f"{int(row.meta.get('micro_iv', 0))}%", em=f"{em:.2f}",
                    kexp=str(row.meta.get("month_exp", "N/A")), kdte=str(row.meta.get("month_dte", 0)), kiv=f"{int(row.meta.get('month_iv', 0))}%", ek=f"{ek:.2f}",
                    sc=row.cal_score, shp=shape, gate=gate_display, tag=tag_str
                ))

            except Exception as e:
                print(f"❌ CRASH on {ticker}: {e}")
                import traceback; traceback.print_exc()
                continue

        if current_rows_for_next_batch:
            self.last_batch_df = pd.DataFrame(current_rows_for_next_batch)
        
        elapsed = time.time() - start_ts
        valid_rows = [item[0] for item in db_results_pack]
        avg_abs_edge = 0.0
        cheap_vol_pct = 0.0
        
        if valid_rows:
            total_abs_edge = sum(abs(r.edge) for r in valid_rows)
            avg_abs_edge = total_abs_edge / len(valid_rows)
            cheap_count = sum(1 for r in valid_rows if r.edge > 0)
            cheap_vol_pct = cheap_count / len(valid_rows)

        self.db.save_scan_session(
            strategy_name, current_vix, len(tickers), avg_abs_edge, cheap_vol_pct, elapsed, db_results_pack
        )
        
        if detail and strict_results:
            print(f"\n🚀 Actionable Blueprints (Tactical Mode)")
            print("-" * WIDTH)
            for row, ctx, bp, gate, dna, reason in strict_results:
                self._print_enhanced_blueprint(bp, row, dna, gate, reason)
        print("-" * WIDTH)

    def _get_gate_status(self, row: ScanRow, bp: Optional[Blueprint], dna_type: str) -> Tuple[str, str]:
        # 1. Hard Kill
        if not bp: return "FORBID", "No Blueprint"
        # [FIX] 如果 bp.error 存在 (如 Zero Liquidity)，直接 FORBID
        if bp.error: return "FORBID", f"Blueprint Error: {bp.error}" 
        
        est_gamma = row.meta.get("est_gamma", 0.0)
        if est_gamma >= self.gamma_hard: return "FORBID", f"Gamma {est_gamma:.3f} >= {self.gamma_hard}"
        
        if dna_type == "CRUSH": return "FORBID", "Momentum: IV CRUSH (-Delta)"

        # 2. Strategy Logic
        tag = row.tag or ""
        em = row.meta.get("edge_micro", 0)
        ek = row.meta.get("edge_month", 0)
        shape = row.meta.get("shape", "FLAT")
        strat_type = str(row.strategy_type if hasattr(row, 'strategy_type') else bp.strategy).upper()
        
        status = "WAIT"
        reason = "Score/Structure suboptimal"

        # LG Logic
        if "LG" in tag or "STRADDLE" in strat_type:
            # [FIX] Spread Check (Orchestrator Level)
            max_spread = row.meta.get("max_spread_pct", None)
            spread_pct = getattr(bp, "meta", {}).get("spread_pct", None) if bp.meta else None
            
            if max_spread is not None and spread_pct is not None:
                if spread_pct > max_spread:
                    return "FORBID", f"Spread {spread_pct:.1%} > {max_spread:.1%}"

            allowed_exec = self.cfg.get("rules", {}).get("lg_allowed_dna_exec", ["PULSE"])
            allowed_limit = self.cfg.get("rules", {}).get("lg_allowed_dna_limit", ["TREND"])
            
            if dna_type in allowed_exec:
                status = "EXEC"; reason = f"Momentum {dna_type} (Aggressive)"
            elif dna_type in allowed_limit:
                status = "LIMIT"; reason = f"Momentum {dna_type} (Passive)"
            else:
                status = "WAIT"; reason = "Market Sleeping (Theta Burn)"

            if em < self.micro_min and ek < self.month_min: 
                return "WAIT", "Edges Too Low"

        # DIAG Logic
        elif "DIAG" in tag:
            if ek < self.month_min:
                status = "WAIT"; reason = f"Back Edge {ek:.2f} < {self.month_min}"
            elif shape in ["FFBS", "STEEP"]:
                status = "EXEC"; reason = "Structure Prime"
            elif shape == "SPIKE":
                status = "WAIT"; reason = "Spike Shape (Front IV too high)"
            else:
                if em < self.micro_min:
                    status = "WAIT"; reason = f"Front Edge {em:.2f} too low"
                else:
                    status = "LIMIT"; reason = "Structure OK"
        
        # Vertical Logic
        elif "BULL" in tag or "BEAR" in tag or "VERT" in tag:
            if row.cal_score >= 60:
                status = "LIMIT"; reason = "Vertical Setup OK"
            else:
                status = "WAIT"; reason = f"Score {row.cal_score} < 60"
        
        # 3. Soft Cap
        if status in ["EXEC", "LIMIT"] and est_gamma >= self.gamma_soft:
            status = "LIMIT"; reason = f"Gamma {est_gamma:.3f} > {self.gamma_soft} (Soft Cap)"
        
        # 4. General Momentum
        if status == "EXEC" and "LG" not in tag:
            if dna_type == "QUIET":
                status = "LIMIT"; reason = "Momentum Quiet (Wait for Pulse)"
        
        # 5. Diagnostic
        if row.symbol in LEV_ETFS and est_gamma > 0.20:
             reason += " [LEV_ETF Risk]"

        return status, reason

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        bp = build_straddle_blueprint(
            symbol=ctx.symbol, underlying=ctx.price, chain=ctx.raw_chain, exp=row.short_exp
        )
        if bp:
            bp.note = "Fallback Gamma Plan"
            return bp
        return Blueprint(ctx.symbol, "STRADDLE", [], 0.0, "Build Failed", error="No Pricing Data")

    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, dna: str, gate: str, reason: str):
        tactic = ""
        if gate == "LIMIT": tactic = f"{Fore.CYAN}[挂单潜伏] Limit @ Mid-$0.05 | {reason}{Style.RESET_ALL}"
        elif gate == "EXEC": tactic = f"{Fore.GREEN}[立即执行] Market/Mid+$0.02 | {reason}{Style.RESET_ALL}"
        elif gate == "WAIT": tactic = f"{Fore.YELLOW}[保持关注] {reason}{Style.RESET_ALL}"

        strat_name = bp.strategy if bp.strategy else "UNKNOWN"
        print(f" {Fore.WHITE}{bp.symbol:<5} {strat_name:<13} | Gate: {gate:<5} | Debit: ${bp.est_debit} | Gamma: {row.meta.get('est_gamma', 0):.4f}")
        print(f"    Edges: Micro {row.meta.get('edge_micro', 0):.2f} / Month {row.meta.get('edge_month', 0):.2f}")
        
        shape = row.meta.get("shape", "")
        mom = row.meta.get("momentum", "QUIET")
        print(f"    Shape: {shape:<8} | Momentum: {mom}")
        
        if "DIAG" in (row.tag or "") and shape == "FFBS":
            print(f"    ✅ {Fore.GREEN}FFBS (Front-Flat Back-Steep): 完美对角线形态{Style.RESET_ALL}")
        
        print(f"    👉 {tactic}")
        
        if bp.legs:
            for leg in bp.legs:
                action_sym = '+' if leg.action == 'BUY' else '-'
                print(f"       {action_sym}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        else:
            print(f"       [ERROR] No Legs: {bp.error}")
        print(f"    {'='*80}")

    def _load_strategy(self, name: str):
        from trade_guardian.domain.registry import StrategyRegistry
        registry = StrategyRegistry(self.cfg, self.policy)
        try: return registry.get(name)
        except: 
            from trade_guardian.strategies.auto import AutoStrategy
            return AutoStrategy(self.cfg, self.policy)