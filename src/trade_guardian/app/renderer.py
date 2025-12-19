from __future__ import annotations
import os
from typing import List, Optional, Any
from trade_guardian.domain.models import ScanRow

# === 1. 定义颜色代码 ===
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m' # 黄色
    FAIL = '\033[91m'    # 红色
    ENDC = '\033[0m'     # 重置颜色
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# 让 Windows 终端支持 ANSI 颜色
os.system('')

class ScanlistRenderer:
    def __init__(self, cfg=None, policy=None, hv_cache_path: Optional[str] = None):
        self.cfg = cfg
        self.policy = policy
        self.hv_cache_path = hv_cache_path

    def _sanitize_int(self, value: Any, default: int = 0) -> int:
        """防御性编程：确保返回的一定是 int"""
        try:
            if isinstance(value, int): return value
            if isinstance(value, str) and value.isdigit(): return int(value)
            if isinstance(value, list): return self._sanitize_int(value[0], default) if value else default
            return default
        except:
            return default

    # [主渲染入口]
    def render(self, 
               strict: List[ScanRow], 
               auto_adjusted: List[ScanRow], 
               watch: List[ScanRow], 
               days: int, 
               min_score: int = 0, 
               max_risk: int = 100, 
               detail: bool = False, 
               universe_size: int = 0,
               top: Any = 0, 
               **kwargs): # 吞掉所有未定义的参数
        
        # 清理 top 参数
        safe_top = self._sanitize_int(top, 0)
        
        # 打印头部信息
        print("")
        print("=" * 95)
        print(f"🧠 {Colors.HEADER}TRADE GUARDIAN :: SCANLIST (days={days}){Colors.ENDC}")
        print("=" * 95)
        
        # 统计信息
        adjusted_list = auto_adjusted if auto_adjusted else []
        total = universe_size if universe_size > 0 else (len(strict) + len(adjusted_list) + len(watch))
        
        print(f"Universe size: {total} | Strict: {len(strict)} | AutoAdjusted: {len(adjusted_list)} | Watch: {len(watch)} | Errors: 0")
        print(f"Strict Filter: score >= {min_score}, short_risk <= {max_risk}")
        if self.hv_cache_path:
            print(f"Throttle: 0.50s/ticker | HV cache: {self.hv_cache_path}")
        
        # 打印表格
        if strict:
            self._print_table(f"✅ {Colors.GREEN}Strict Candidates (actionable now){Colors.ENDC}", strict)
            if detail:
                self._print_details("Top details (per-row explain)", strict)
                # 打印蓝图 (去掉了 Strategy #3 的文字)
                self._print_blueprints(f"🚀 {Colors.CYAN}Actionable Blueprints{Colors.ENDC}", strict)

        if adjusted_list:
            self._print_table("🤖 Auto-Adjusted Candidates", adjusted_list)

        if watch:
            self._print_table("👀 Watchlist", watch)

    # [诊断信息入口]
    def render_diagnostics(self, strict: List[ScanRow], **kwargs):
        if not strict: return

        print(f"\n🧾 Diagnostics")
        avg_score = sum(r.cal_score for r in strict) / len(strict)
        
        # 计算平均 Edge
        valid_edges = [r.edge for r in strict if r.edge > 0]
        avg_edge = sum(valid_edges) / len(valid_edges) if valid_edges else 0.0
        
        print(f"   • Avg Score: {avg_score:.1f} | Avg Edge: {avg_edge:.2f}x")

    # [内部 helper] 打印表格
    def _print_table(self, title: str, rows: List[ScanRow]):
        if not rows: return
        if title: print(f"\n{title}")
        
        header = f"{'Sym':<6} {'Px':<7} {'ShortExp':<10} {'ShortDTE':>8} {'ShortIV':>8} {'BaseIV':>8} {'Edge':>7} {'HV%':>5} {'Score':>7} {'Risk':>6} {'Tag':<11}"
        print(header)
        print("-" * len(header))
        
        for r in rows:
            # IV 修正：除以 100
            short_iv_val = r.short_iv / 100.0
            base_iv_val = r.base_iv / 100.0
            
            row_str = (
                f"{r.symbol:<6} "
                f"{r.price:<7.2f} "
                f"{r.short_exp:<10} "
                f"{r.short_dte:>8} "
                f"{short_iv_val:>8.1%} "
                f"{base_iv_val:>8.1%} "
                f"{r.edge:>6.2f}x "
                f"{r.hv_rank:>4.0f}% "
                f"{r.cal_score:>7} "
                f"{r.short_risk:>6} "
                f"{r.tag:<11}"
            )
            print(row_str)

    # [内部 helper] 打印详情
    def _print_details(self, title: str, rows: List[ScanRow]):
        print(f"\n{title}")
        print("Explain legend")
        print("  score parts: b=base, rg=regime, ed=edge, hv=HV-rank slot, cv=curvature, pen=penalties")
        print("  risk  parts: b=base, dte=time-to-expiry, gm=gamma proxy, cv=curvature risk, rg=regime risk, pen=penalties")
        
        for r in rows:
            bd = r.score_breakdown
            rbd = r.risk_breakdown
            print(f"\n  {Colors.BOLD}{r.symbol:<6}{Colors.ENDC} score={r.cal_score:<3} [b{bd.base:+} rg{bd.regime:+} ed{bd.edge:+} hv{bd.hv:+} cv{bd.curvature:+} pen{bd.penalties:+}] | edge={r.edge:.2f}x tag={r.tag} hv={r.hv_rank:.0f}%")
            print(f"         risk={r.short_risk:<3} [b{rbd.base:+} dte{rbd.dte:+} gm{rbd.gamma:+} cv{rbd.curvature:+} rg{rbd.regime:+}] | short={r.short_exp} d{r.short_dte}")

    # [内部 helper] 打印蓝图 (包含 Greeks)
    def _print_blueprints(self, title: str, rows: List[ScanRow]):
        valid_rows = [r for r in rows if getattr(r, 'blueprint', None)]
        if not valid_rows: return

        print(f"\n{title}")
        print("-" * 95)
        for r in valid_rows:
            bp = r.blueprint
            
            # 摘要行
            line = bp.one_liner()
            if "est_debit=" in line:
                parts = line.split("est_debit=")
                line = f"{parts[0]}{Colors.CYAN}est_debit={parts[1]}{Colors.ENDC}"
            print(f"  {line}")
            
            # Note 行
            note = getattr(bp, "note", "")
            if note:
                if "WARNING" in note or "Risk" in note:
                    print(f"    Note: {Colors.FAIL}{note}{Colors.ENDC}")
                elif "Healthy" in note:
                    print(f"    Note: {Colors.GREEN}{note}{Colors.ENDC}")
                else:
                    print(f"    Note: {note}")

            # 腿部详情 (带 Greeks)
            if hasattr(bp, "short_exp") and hasattr(bp, "long_exp"):
                # Diagonal / PMCC / Calendar
                if hasattr(bp, "short_strike") and hasattr(bp, "long_strike"):
                     # PMCC
                     s_delta = bp.short_greeks.get("delta", 0) if getattr(bp, "short_greeks", None) else 0
                     l_delta = bp.long_greeks.get("delta", 0) if getattr(bp, "long_greeks", None) else 0
                     print(f"    Legs: +{bp.long_exp} ({bp.long_strike}C) [Δ{l_delta:.2f}]")
                     print(f"          -{bp.short_exp} ({bp.short_strike}C) [Δ{s_delta:.2f}]")
                else:
                     # Calendar
                     print(f"    Legs: -{bp.short_exp} / +{bp.long_exp} @ Strike {bp.strike}")
            
            elif hasattr(bp, "exp"):
                # Straddle
                atm_gamma = bp.greeks.get("gamma", 0) if getattr(bp, "greeks", None) else 0
                atm_delta = bp.greeks.get("delta", 0) if getattr(bp, "greeks", None) else 0
                print(f"    Legs: +{bp.exp} CALL & PUT @ Strike {bp.strike} [Δ{atm_delta:.2f} Γ{atm_gamma:.3f}]")
            
            else:
                print(f"    Legs: (Unknown structure)")

        print("-" * 95)
        print("")