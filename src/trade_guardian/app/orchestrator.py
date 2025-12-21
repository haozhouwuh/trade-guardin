from __future__ import annotations
import os
import sys
import time
import pandas as pd
import traceback
from typing import List, Tuple, Optional, Any
from colorama import Fore, Style

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg
from trade_guardian.app.persistence import PersistenceManager
from trade_guardian.strategies.blueprint import build_straddle_blueprint 

# --- [交易员底线参数] ---
MICRO_MIN = 0.10
MONTH_MIN = 0.15

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
        
        try:
            vix_q = self.client.get_quote("$VIX")
            current_vix = vix_q.get("lastPrice", 0.0) 
        except: current_vix = 0.0
        
        tickers = self._get_universe()
        if limit: tickers = tickers[:limit]

        db_results_pack = []  
        all_rows_for_stats = [] 
        current_rows_for_next_batch = [] 
        strict_results = [] 
        
        # DNA -> Shape (Display Structure Shape)
        FMT = "{sym:<5} {px:<7} {sexp:<11} {sdte:<3} {siv:>6} | {mexp:<11} {mdte:<3} {miv:>6} {em:>5} | {kexp:<11} {kdte:<3} {kiv:>6} {ek:>5} | {sc:>4} {shp:<8} {gate:<6}   {tag:<8}"
        
        HEADER = FMT.format(
            sym="Sym", px="Px", sexp="ShortExp", sdte="D", siv="S_IV",
            mexp="MicroExp", mdte="D", miv="M_IV", em="EdgM",
            kexp="MonthExp", kdte="D", kiv="K_IV", ek="EdgK",
            sc="Scr", shp="Shape", gate="Gate", tag="Tag"
        )
        WIDTH = len(HEADER)

        print("\n" + "=" * WIDTH)
        print(f"🧠 TRADE GUARDIAN :: GRADUATION BUILD | VIX: {current_vix:.2f}")
        print("-" * WIDTH)
        print(HEADER)
        print("-" * WIDTH)

        for ticker in tickers:
            try:
                # 1. 构建上下文
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: continue
                
                # 2. 策略路由
                strategy = self._load_strategy("auto") 
                row = strategy.evaluate(ctx)
                if not row: continue

                # 3. 动能计算 (Momentum)
                iv_diff = 0.0
                if self.last_batch_df is not None:
                    prev = self.last_batch_df[self.last_batch_df['symbol'] == row.symbol]
                    if not prev.empty:
                        iv_diff = row.short_iv - prev.iloc[0]['iv']
                
                mom_type = "QUIET"
                if iv_diff > 2.0: mom_type = "PULSE"
                elif iv_diff > 0.5: mom_type = "TREND"
                elif iv_diff < -1.0: mom_type = "CRUSH"
                
                row.meta["delta_15m"] = iv_diff
                row.meta["momentum"] = mom_type

                # 4. 形态分类 (Shape Classifier)
                # FFBS: Front-Flat Back-Steep (Ideal for Diagonal)
                tsf = ctx.tsf or {}
                regime = str(tsf.get("regime", "FLAT"))
                is_squeeze = bool(tsf.get("is_squeeze", False))
                curvature = str(tsf.get("curvature", "NORMAL"))
                
                em = float(row.meta.get("edge_micro", 0) or 0)
                ek = float(row.meta.get("edge_month", 0) or 0)
                
                shape = "FLAT"
                if regime == "BACKWARDATION":
                    shape = "BACKWARD"
                elif ek >= 0.20 and em < 0.08:
                    shape = "FFBS" # 黄金对角线形态
                elif is_squeeze or curvature == "SPIKY_FRONT" or em >= 0.12:
                    shape = "SPIKE"
                elif ek >= 0.15:
                    shape = "STEEP"
                else:
                    shape = "FLAT"
                
                row.meta["shape"] = shape
                
                # 5. 获取蓝图
                bp = getattr(row, 'blueprint', None)
                if not bp:
                    bp = self.plan(ctx, row) 
                
                # 6. 风控门槛 (Gate V6)
                gate = self._get_gate_status(row, bp, mom_type) 
                
                db_results_pack.append((row, ctx, bp, gate)) 
                all_rows_for_stats.append(row)
                current_rows_for_next_batch.append({'symbol': row.symbol, 'iv': row.short_iv})
                
                if gate != "FORBID":
                    strict_results.append((row, ctx, bp, gate, mom_type))

                # 7. 打印
                if gate == "EXEC": g_color = Fore.GREEN
                elif gate == "LIMIT": g_color = Fore.CYAN
                elif gate == "FORBID": g_color = Fore.RED
                else: g_color = Fore.YELLOW
                
                gate_display = f"{g_color}{gate:<6}{Style.RESET_ALL}"
                
                print(FMT.format(
                    sym=row.symbol,
                    px=f"{row.price:.1f}",
                    sexp=row.short_exp,
                    sdte=row.short_dte,
                    siv=f"{int(row.short_iv)}%",
                    mexp=str(row.meta.get("micro_exp", "N/A")),
                    mdte=str(row.meta.get("micro_dte", 0)),
                    miv=f"{int(row.meta.get('micro_iv', 0))}%",
                    em=f"{em:.2f}",
                    kexp=str(row.meta.get("month_exp", "N/A")),
                    kdte=str(row.meta.get("month_dte", 0)),
                    kiv=f"{int(row.meta.get('month_iv', 0))}%",
                    ek=f"{ek:.2f}",
                    sc=row.cal_score,
                    shp=shape, # 显示 Shape
                    gate=gate_display, 
                    tag=row.tag
                ))

            except Exception as e:
                print(f"❌ CRASH on {ticker}: {e}")
                traceback.print_exc()
                continue

        self.last_batch_df = pd.DataFrame(current_rows_for_next_batch)
        self.db.save_scan_session(strategy_name, current_vix, len(tickers), 0.0, 0.0, 0.0, db_results_pack)
        
        if detail and strict_results:
            print(f"\n🚀 Actionable Blueprints (Tactical Mode)")
            print("-" * WIDTH)
            for row, ctx, bp, gate, dna in strict_results:
                self._print_enhanced_blueprint(bp, row, dna, gate)
        print("-" * WIDTH)

    def _get_gate_status(self, row: ScanRow, bp: Optional[Blueprint], dna_type: str) -> str:
        est_gamma = row.meta.get("est_gamma", 0.0)
        
        # --- Layer 1: Hard Kill (绝对风控) ---
        if not bp or bp.error: return "FORBID"
        if est_gamma >= 0.30: return "FORBID" 
        if dna_type == "CRUSH": return "FORBID" 
        
        em = row.meta.get("edge_micro", 0)
        ek = row.meta.get("edge_month", 0)
        shape = row.meta.get("shape", "FLAT")
        tag = row.tag or ""
        short_dte = row.short_dte
        
        # --- Layer 2: Strategy & Shape Gate (结构门槛) ---
        
        if "DIAG" in tag:
            # [DIAG 核心] 看后端结构 (ek)
            if ek < MONTH_MIN:
                return "WAIT"
            
            # [形态特判]
            # A. FFBS / STEEP: 完美形态，豁免前端微结构要求 (em)
            if shape in ["FFBS", "STEEP"]:
                pass 
            
            # B. SPIKE: 前端挤压，风险极高 -> 降级保护 (Rule #4)
            # 如果短腿 <= 7 DTE 且动能不强，强制 WAIT，不允许 LIMIT 被动吃 Gamma
            elif shape == "SPIKE":
                if short_dte <= 7 and dna_type == "QUIET":
                    return "WAIT"
                # 如果是 SPIKE 但 em 极差 (理论上 SPIKE em 应该高，这里是兜底)
                if em < MICRO_MIN:
                    return "WAIT"

            # C. 其他形态 (FLAT/MILD): 必须双边达标
            else:
                if em < MICRO_MIN:
                    return "WAIT"

        else:
            # [LG 核心] 前端不能太烂，或者纯博低波
            # 如果 em 和 ek 双低，且没有特殊原因，WAIT
            if em < MICRO_MIN and ek < MONTH_MIN:
                return "WAIT"

        # --- Layer 3: Momentum Gate (动能执行) ---
        if dna_type in ["PULSE", "TREND"]:
            return "EXEC"
        else:
            return "LIMIT"
        

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        bp = build_straddle_blueprint(
            symbol=ctx.symbol,
            underlying=ctx.price,
            chain=ctx.raw_chain,
            exp=row.short_exp
        )
        if bp:
            bp.note = "Fallback Gamma Plan"
            return bp
        return Blueprint(ctx.symbol, "STRADDLE", [], 0.0, "Build Failed", error="No Pricing Data")

    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, dna: str, gate: str):
        tactic = ""
        if gate == "LIMIT":
            tactic = f"{Fore.CYAN}[挂单潜伏] Limit @ Mid-$0.05 | 等待 DNA 激活{Style.RESET_ALL}"
        elif gate == "EXEC":
            tactic = f"{Fore.GREEN}[立即执行] Market/Mid+$0.02 | 动能确立{Style.RESET_ALL}"
        elif gate == "WAIT":
             tactic = f"{Fore.YELLOW}[保持关注] 尚未达到入场标准{Style.RESET_ALL}"

        print(f" {Fore.WHITE}{bp.symbol:<5} | Gate: {gate:<5} | Debit: ${bp.est_debit} | Gamma: {row.meta.get('est_gamma', 0):.4f}")
        print(f"    Edges: Micro {row.meta.get('edge_micro', 0):.2f} / Month {row.meta.get('edge_month', 0):.2f}")
        
        # [新增] 形态解释
        shape = row.meta.get("shape", "")
        mom = row.meta.get("momentum", "QUIET")
        print(f"    Shape: {shape:<8} | Momentum: {mom}")
        
        if "DIAG" in (row.tag or "") and shape == "FFBS":
            print(f"    ✅ {Fore.GREEN}FFBS (Front-Flat Back-Steep): 完美对角线形态，前端安稳，后端高溢价。{Style.RESET_ALL}")
        
        print(f"    👉 {tactic}")
        
        if bp.legs:
            for leg in bp.legs:
                action_sym = '+' if leg.action == 'BUY' else '-'
                print(f"       {action_sym}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        else:
            print(f"       [ERROR] No Legs: {bp.error}")
        print(f"    {'='*80}")

    def _load_strategy(self, name: str):
        from trade_guardian.strategies.auto import AutoStrategy
        return AutoStrategy(self.cfg, self.policy)