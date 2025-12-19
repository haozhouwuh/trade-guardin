from __future__ import annotations
import os
import sys
import time
import pandas as pd
from typing import List, Tuple, Optional, Any
from colorama import Fore, Style

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg
from trade_guardian.app.persistence import PersistenceManager

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
        
        # ✅ 最终对齐修复：增加间距，单独处理颜色列的宽度
        FMT = "{sym:<5} {px:<7} {sexp:<11} {sdte:<3} {siv:>6} | {mexp:<11} {mdte:<3} {miv:>6} {em:>5} | {kexp:<11} {kdte:<3} {kiv:>6} {ek:>5} | {sc:>4} {dna:<6} {gate:<6}   {tag:<8}"
        
        HEADER = FMT.format(
            sym="Sym", px="Px", sexp="ShortExp", sdte="D", siv="S_IV",
            mexp="MicroExp", mdte="D", miv="M_IV", em="EdgM",
            kexp="MonthExp", kdte="D", kiv="K_IV", ek="EdgK",
            sc="Scr", dna="DNA", gate="Gate", tag="Tag"
        )
        WIDTH = len(HEADER)

        print("\n" + "=" * WIDTH)
        print(f"🧠 TRADE GUARDIAN :: GRADUATION BUILD | VIX: {current_vix:.2f}")
        print("-" * WIDTH)
        print(HEADER)
        print("-" * WIDTH)

        for ticker in tickers:
            try:
                ctx = self.client.build_context(ticker, days=days)
                if not ctx: continue
                
                strategy = self._load_strategy("long_gamma")
                row = strategy.evaluate(ctx)
                if not row: continue

                # 动能计算
                iv_diff = 0.0
                if self.last_batch_df is not None:
                    prev = self.last_batch_df[self.last_batch_df['symbol'] == row.symbol]
                    if not prev.empty:
                        iv_diff = row.short_iv - prev.iloc[0]['iv']
                
                dna_type = "QUIET"
                if iv_diff > 2.0: dna_type = "PULSE"
                elif iv_diff > 0.5: dna_type = "TREND"
                elif iv_diff < -1.0: dna_type = "CRUSH"

                row.meta["delta_15m"] = iv_diff
                bp = self.plan(ctx, row)
                gate = self._get_gate_status(row, bp, dna_type) 
                
                db_results_pack.append((row, ctx, bp, gate)) 
                all_rows_for_stats.append(row)
                current_rows_for_next_batch.append({'symbol': row.symbol, 'iv': row.short_iv})
                
                if gate != "FORBID":
                    strict_results.append((row, ctx, bp, gate, dna_type))

                # 颜色逻辑
                if gate == "EXEC": g_color = Fore.GREEN
                elif gate == "LIMIT": g_color = Fore.CYAN
                elif gate == "FORBID": g_color = Fore.RED
                else: g_color = Fore.YELLOW
                
                # ✅ 核心修复：先手动填充空格，再上色，确保视觉宽度一致
                # 将 Gate 强制填充到 6 字符宽，然后再包颜色代码
                gate_padded = f"{gate:<6}"
                gate_display = f"{g_color}{gate_padded}{Style.RESET_ALL}"
                
                # 数据提取
                m_iv_val = row.meta.get('micro_iv', 0) or 0
                k_iv_val = row.meta.get('month_iv', 0) or 0
                em_val = row.meta.get('edge_micro', 0) or 0
                ek_val = row.meta.get('edge_month', 0) or 0

                print(FMT.format(
                    sym=row.symbol,
                    px=f"{row.price:.1f}",
                    sexp=row.short_exp,
                    sdte=row.short_dte,
                    siv=f"{int(row.short_iv)}%",
                    
                    mexp=str(row.meta.get("micro_exp", "N/A")),
                    mdte=str(row.meta.get("micro_dte", 0)),
                    miv=f"{int(m_iv_val)}%",
                    em=f"{em_val:.2f}",
                    
                    kexp=str(row.meta.get("month_exp", "N/A")),
                    kdte=str(row.meta.get("month_dte", 0)),
                    kiv=f"{int(k_iv_val)}%",
                    ek=f"{ek_val:.2f}",
                    
                    sc=row.cal_score,
                    dna=dna_type,
                    gate=gate_display, # 使用修复后的 Gate 显示
                    tag=row.tag
                ))

            except Exception as e:
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
        
        # 1. 绝对风控
        if not bp or bp.error or est_gamma >= 0.25: return "FORBID"
        if dna_type == "CRUSH": return "FORBID" 
        
        em = row.meta.get("edge_micro", 0)
        ek = row.meta.get("edge_month", 0)
        
        # 2. 结构门槛
        if em < MICRO_MIN or ek < MONTH_MIN:
            return "WAIT"

        # 3. 结构达标
        if dna_type in ["PULSE", "TREND"]:
            return "EXEC"
        else:
            return "LIMIT"

    def plan(self, ctx: Context, row: ScanRow) -> Optional[Blueprint]:
        symbol = ctx.symbol
        atm_strike = row.meta.get("strike", round(row.price, 1))
        target_exp_key = f"{row.short_exp}:{row.short_dte}"
        est_debit = -999999.99
        
        try:
            call_map = ctx.raw_chain.get("callExpDateMap", {})
            put_map = ctx.raw_chain.get("putExpDateMap", {})
            strike_key = f"{float(atm_strike):g}"
            
            if strike_key not in call_map.get(target_exp_key, {}):
                keys = sorted([float(k) for k in call_map.get(target_exp_key, {}).keys()])
                if keys: strike_key = f"{min(keys, key=lambda x: abs(x - atm_strike)):g}"

            c_strikes = call_map.get(target_exp_key, {})
            p_strikes = put_map.get(target_exp_key, {})
            
            if strike_key in c_strikes and strike_key in p_strikes:
                c_c = c_strikes[strike_key][0]
                p_c = p_strikes[strike_key][0]
                call_price = (float(c_c.get("bid", 0)) + float(c_c.get("ask", 0))) / 2.0
                put_price = (float(p_c.get("bid", 0)) + float(p_c.get("ask", 0))) / 2.0
                if call_price > 0 and put_price > 0:
                    est_debit = call_price + put_price
        except: pass

        legs = [OrderLeg(symbol, "BUY", 1, row.short_exp, float(atm_strike), "CALL"), 
                OrderLeg(symbol, "BUY", 1, row.short_exp, float(atm_strike), "PUT")]
        return Blueprint(symbol, "STRADDLE", legs, round(est_debit, 2), "Gamma Plan")

    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, dna: str, gate: str):
        tactic = ""
        if gate == "LIMIT":
            tactic = f"{Fore.CYAN}[挂单潜伏] 建议 Limit @ Mid-$0.05 | 触发条件: 等待 DNA 激活{Style.RESET_ALL}"
        elif gate == "EXEC":
            tactic = f"{Fore.GREEN}[立即执行] 建议 Market 或 Mid+$0.02 | 触发条件: 动能确立{Style.RESET_ALL}"
        elif gate == "WAIT":
             tactic = f"{Fore.YELLOW}[保持关注] 尚未达到入场标准{Style.RESET_ALL}"

        print(f" {Fore.WHITE}{bp.symbol:<5} | Gate: {gate:<5} | Debit: ${bp.est_debit} | Gamma: {row.meta.get('est_gamma', 0):.4f}")
        print(f"    Edges: Micro {row.meta.get('edge_micro', 0):.2f} / Month {row.meta.get('edge_month', 0):.2f}")
        print(f"    👉 {tactic}")
        for leg in bp.legs:
            print(f"       {'+' if leg.action == 'BUY' else '-'}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        print(f"    {'='*80}")

    def _load_strategy(self, name: str):
        from trade_guardian.strategies.long_gamma import LongGammaStrategy
        return LongGammaStrategy(self.cfg, self.policy)