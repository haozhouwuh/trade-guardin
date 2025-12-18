from __future__ import annotations

from typing import Any, List, Optional, Tuple

try:
    from colorama import Fore, Style
except Exception:  # pragma: no cover
    class _Dummy:
        RESET_ALL = ""
    class _Fore(_Dummy):
        CYAN = ""
        GREEN = ""
        YELLOW = ""
        RED = ""
        MAGENTA = ""
    class _Style(_Dummy):
        RESET_ALL = ""
    Fore = _Fore()
    Style = _Style()

try:
    from tabulate import tabulate
except Exception:  # pragma: no cover
    tabulate = None

from trade_guardian.domain.models import ScanRow
from trade_guardian.domain.policy import ShortLegPolicy


class ScanlistRenderer:
    def __init__(self, cfg: dict, policy: ShortLegPolicy, hv_cache_path: str = ""):
        self.cfg = cfg
        self.policy = policy
        self.hv_cache_path = hv_cache_path

    # ---------------------------
    # formatting helpers
    # ---------------------------
    def _fmt_pct(self, x: float, digits: int = 1) -> str:
        """
        Accept both:
        - decimal IV: 0.129 -> 12.9%
        - percent IV: 12.9  -> 12.9%
        Heuristic: values > 3 are treated as already-percent.
        """
        try:
            v = float(x)
        except Exception:
            return "N/A"

        if v != v:  # NaN
            return "N/A"

        pct = v if abs(v) > 3.0 else (v * 100.0)
        return f"{pct:.{digits}f}%"

    def _fmt_float(self, x: Any, digits: int = 2) -> str:
        try:
            return f"{float(x):.{digits}f}"
        except Exception:
            return "N/A"

    def _fmt_edge(self, x: float) -> str:
        try:
            return f"{float(x):.2f}x"
        except Exception:
            return "N/A"

    def _policy_probe_range_str(self) -> str:
        """
        Never rely on internal policy attributes.
        Use stable method: probe_ranks().
        """
        try:
            ranks = list(self.policy.probe_ranks())
        except Exception:
            ranks = [getattr(self.policy, "base_rank", 1)]

        if not ranks:
            return "-"

        lo = min(ranks)
        hi = max(ranks)
        return f"{lo}..{hi}"

    # ---------------------------
    # tables
    # ---------------------------
    def _print_table(self, title: str, rows: List[ScanRow], kind: str):
        """
        kind:
          - "base": show ScanRow chosen short leg
          - "auto": show recommendation fields (rec_*)
        """
        if not rows:
            print(f"{title}\n  (none)\n")
            return

        if tabulate is None:
            print(title)
            for r in rows:
                print(f"  {r.symbol} score={r.cal_score} risk={r.short_risk} tag={r.tag}")
            print("")
            return

        if kind == "auto":
            headers = [
                "Sym", "Px",
                "ShortExp", "ShortDTE", "ShortIV",
                "RecExp", "RecDTE", "RecIV",
                "RecEdge", "RecScore", "RecRisk", "RecTag",
            ]
            table = []
            for r in rows:
                rec = r.rec
                table.append([
                    r.symbol,
                    self._fmt_float(r.price, 2),
                    r.short_exp, r.short_dte, self._fmt_pct(r.short_iv, 1),
                    rec.rec_exp if rec else "-",
                    rec.rec_dte if rec else "-",
                    self._fmt_pct(rec.rec_iv, 1) if rec else "-",
                    self._fmt_edge(rec.rec_edge) if rec else "-",
                    rec.rec_score if rec else "-",
                    rec.rec_risk if rec else "-",
                    rec.rec_tag if rec else "-",
                ])
            print(title)
            print(tabulate(table, headers=headers, tablefmt="simple"))
            print("")
            return

        headers = ["Sym", "Px", "ShortExp", "ShortDTE", "ShortIV", "BaseIV", "Edge", "HV%", "Score", "Risk", "Tag"]
        table = []
        for r in rows:
            table.append([
                r.symbol,
                self._fmt_float(r.price, 2),
                r.short_exp,
                r.short_dte,
                self._fmt_pct(r.short_iv, 1),
                self._fmt_pct(r.base_iv, 1),
                self._fmt_edge(r.edge),
                f"{r.hv_rank:.0f}%",
                r.cal_score,
                r.short_risk,
                r.tag,
            ])
        print(title)
        print(tabulate(table, headers=headers, tablefmt="simple"))
        print("")

    # ---------------------------
    # details
    # ---------------------------
    def _detail_lines(self, title: str, rows: List[ScanRow], limit: int = 15):
        if not rows:
            return

        print(f"{Fore.CYAN}{title} details (per-row explain){Style.RESET_ALL}")

        print("Explain legend")
        print("  score parts: b=base, rg=regime, ed=edge, hv=HV-rank slot, cv=curvature, pen=penalties")
        print("  risk  parts: b=base, dte=time-to-expiry, gm=gamma proxy, cv=curvature risk, rg=regime risk, pen=penalties")
        print("")

        for r in rows[:limit]:
            bd = r.score_breakdown
            pen = getattr(bd, "penalties", 0)
            pen_str = f" pen{pen:+d}" if pen else ""

            print(
                f"  {r.symbol:<6} score={r.cal_score:<3d} "
                f"[b{bd.base:+d} rg{bd.regime:+d} ed{bd.edge:+d} "
                f"hv{bd.hv:+d} cv{bd.curvature:+d}{pen_str}] "
                f"| edge={self._fmt_float(r.edge)}x tag={r.tag} hv={r.hv_rank:.0f}%"
            )

            rbd = getattr(r, "risk_breakdown", None)
            if rbd is None:
                print(f"         risk={r.short_risk:<3d} | short={r.short_exp} d{r.short_dte}")
                continue

            r_pen = getattr(rbd, "penalties", 0)
            r_pen_str = f" pen{r_pen:+d}" if r_pen else ""

            squeeze = getattr(r, "squeeze_ratio", None)
            squeeze_str = f" | squeeze={self._fmt_float(squeeze, 2)}x" if isinstance(squeeze, (int, float)) else ""

            print(
                f"         risk={r.short_risk:<3d} "
                f"[b{getattr(rbd,'base',0):+d} dte{getattr(rbd,'dte',0):+d} "
                f"gm{getattr(rbd,'gamma',0):+d} cv{getattr(rbd,'curv',0):+d} "
                f"rg{getattr(rbd,'regime',0):+d}{r_pen_str}] "
                f"| short={r.short_exp} d{r.short_dte}{squeeze_str}"
            )

            if "S" in str(r.tag) and getattr(rbd, "curv", 0) == 0:
                thr = getattr(r, "squeeze_threshold", None)
                thr_str = f"{self._fmt_float(thr, 2)}x" if isinstance(thr, (int, float)) else "N/A"
                sq_str = f"{self._fmt_float(squeeze, 2)}x" if isinstance(squeeze, (int, float)) else "N/A"
                print(f"         note: SPIKY_FRONT tag detected, but curvature risk not added (squeeze={sq_str}, threshold={thr_str})")

        print("")

    # ---------------------------
    # main render
    # ---------------------------
    def render(
        self,
        *,
        days: int,
        universe_size: int,
        min_score: int,
        max_risk: int,
        strict: List[ScanRow],
        auto_adjusted: List[ScanRow],
        watch: List[ScanRow],
        top: List[ScanRow],
        errors: List[Tuple[str, str]],
        detail: bool = False,
    ):
        print("")
        print("=" * 95)
        print(f"🧠 TRADE GUARDIAN :: SCANLIST (days={days})")
        print("=" * 95)

        probe_range = self._policy_probe_range_str()
        base_rank = getattr(self.policy, "base_rank", "N/A")
        min_dte = getattr(self.policy, "min_dte", "N/A")

        print(f"Short leg policy: base_rank={base_rank}, min_dte={min_dte}, probe_ranks={probe_range}")
        print(f"Universe size: {universe_size} | Strict: {len(strict)} | AutoAdjusted: {len(auto_adjusted)} | Watch: {len(watch)} | Errors: {len(errors)}")
        print(f"Strict Filter: cal_score >= {min_score}, short_risk <= {max_risk}")
        throttle = float(self.cfg.get("scan", {}).get("throttle_sec", 0.5))
        print(f"Throttle: {throttle:.2f}s/ticker | HV cache: {self.hv_cache_path}")
        print("")

        self._print_table(f"{Fore.GREEN}✅ Strict Candidates (actionable now){Style.RESET_ALL}", strict, kind="base")
        self._print_table(f"{Fore.MAGENTA}🤖 Auto-Adjusted Candidates (recommended rank within probe range){Style.RESET_ALL}", auto_adjusted, kind="auto")
        self._print_table(f"{Fore.YELLOW}👀 Watchlist (score OK but still risky within probe range){Style.RESET_ALL}", watch, kind="base")
        self._print_table(f"{Fore.CYAN}🏆 Top Overall (ranked by score + edge + lower risk){Style.RESET_ALL}", top, kind="base")

        if detail and top:
            self._detail_lines("Top", top, limit=15)

        print("Tag Legend")
        print("  • First letter (Regime): F=FLAT, C=CONTANGO, B=BACKWARDATION")
        print("  • S suffix (Curvature): S=SPIKY_FRONT")
        print("  • Example: FS = Flat + Spiky front; CS = Contango + Spiky front")
        print("")

        # [新增] 打印交易蓝图
        self._print_blueprints("🚀 Actionable Blueprints (Strategy #3)", strict)

        if errors:
            print(f"{Fore.RED}Errors (first 15):{Style.RESET_ALL}")
            for sym, msg in errors[:15]:
                print(f"  - {sym}: {msg}")
            print("")

    # ---------------------------
    # diagnostics
    # ---------------------------
    def render_diagnostics(self, *, rows: List[ScanRow], **_kwargs):
        """
        Backward/forward compatible:
        orchestrator might pass min_score/max_risk/etc.
        We accept them and ignore here.
        """
        if not rows:
            return

        def avg(nums: List[float]) -> float:
            return sum(nums) / max(1, len(nums))

        avg_score = avg([float(r.cal_score) for r in rows])
        avg_risk = avg([float(r.short_risk) for r in rows])
        avg_dte = avg([float(r.short_dte) for r in rows])
        avg_edge = avg([float(r.edge) for r in rows])

        bds = [r.score_breakdown for r in rows if getattr(r, "score_breakdown", None) is not None]
        bd_base = avg([float(bd.base) for bd in bds]) if bds else 0.0
        bd_reg = avg([float(bd.regime) for bd in bds]) if bds else 0.0
        bd_edge = avg([float(bd.edge) for bd in bds]) if bds else 0.0
        bd_hv = avg([float(bd.hv) for bd in bds]) if bds else 0.0
        bd_curv = avg([float(bd.curvature) for bd in bds]) if bds else 0.0

        rbds = [getattr(r, "risk_breakdown", None) for r in rows]
        rbds = [x for x in rbds if x is not None]
        rbd_base = avg([float(getattr(x, "base", 0)) for x in rbds]) if rbds else 0.0
        rbd_dte = avg([float(getattr(x, "dte", 0)) for x in rbds]) if rbds else 0.0
        rbd_gm = avg([float(getattr(x, "gamma", 0)) for x in rbds]) if rbds else 0.0
        rbd_cv = avg([float(getattr(x, "curv", 0)) for x in rbds]) if rbds else 0.0
        rbd_rg = avg([float(getattr(x, "regime", 0)) for x in rbds]) if rbds else 0.0

        print(f"{Fore.CYAN}🧾 Diagnostics{Style.RESET_ALL}")
        print(f"   • Avg CalScore: {avg_score:.1f} | Avg ShortRisk: {avg_risk:.1f} | Avg ShortDTE: {avg_dte:.1f} | Avg Edge(S/B): {avg_edge:.2f}x")
        print(f"   • Score avg breakdown: base {bd_base:+.1f} | regime {bd_reg:+.1f} | edge {bd_edge:+.1f} | hv {bd_hv:+.1f} | curv {bd_curv:+.1f}")
        if rbds:
            print(f"   • Risk avg breakdown:  base {rbd_base:+.1f} | dte {rbd_dte:+.1f} | gamma {rbd_gm:+.1f} | curv {rbd_cv:+.1f} | regime {rbd_rg:+.1f}")
        print("")

    # [新增辅助方法]
    def _print_blueprints(self, title: str, rows: List[ScanRow]):
        valid_rows = [r for r in rows if getattr(r, 'blueprint', None)]
        if not valid_rows:
            return

        print(title)
        print("-" * 80)
        for r in valid_rows:
            bp = r.blueprint
            
            # 打印第一行摘要 (所有 Blueprint 都有 one_liner 方法)
            print(f"  {bp.one_liner()}")
            
            # 打印 Note
            if getattr(bp, "note", ""):
                print(f"    Note: {bp.note}")

            # 针对不同类型的 Blueprint 打印详细腿部信息
            # Case A: Calendar (有 short_exp / long_exp)
            if hasattr(bp, "short_exp") and hasattr(bp, "long_exp"):
                print(f"    Legs: -{bp.short_exp} / +{bp.long_exp} @ Strike {bp.strike}")
            
            # Case B: Straddle (只有 exp)
            elif hasattr(bp, "exp"):
                print(f"    Legs: +{bp.exp} CALL & PUT @ Strike {bp.strike}")
            
            # Case C: 未知类型
            else:
                print(f"    Legs: (Unknown structure)")

        print("-" * 80)
        print("")

