from __future__ import annotations

import os
from typing import List, Tuple

import numpy as np
from tabulate import tabulate
from colorama import Fore, Style

from trade_guardian.domain.models import ScanRow
from trade_guardian.domain.policy import ShortLegPolicy


class ScanlistRenderer:
    def __init__(self, cfg: dict, policy: ShortLegPolicy, hv_cache_path: str):
        self.cfg = cfg
        self.policy = policy
        self.hv_cache_path = hv_cache_path

    # -------------------- utils --------------------

    @staticmethod
    def _fmt_float(x, n: int = 2) -> str:
        try:
            return f"{x:.{n}f}"
        except Exception:
            return "N/A"

    @staticmethod
    def _tab(headers, rows):
        if not rows:
            print(Fore.YELLOW + "  (none)\n" + Style.RESET_ALL)
            return
        print(tabulate(rows, headers=headers, tablefmt="simple"))
        print("")

    @staticmethod
    def _tag_legend():
        print(f"{Fore.CYAN}Tag Legend{Style.RESET_ALL}")
        print("  • First letter (Regime): F=FLAT, C=CONTANGO, B=BACKWARDATION")
        print("  • S suffix (Curvature): S=SPIKY_FRONT")
        print("  • Example: FS = Flat + Spiky front; CS = Contango + Spiky front\n")

    @staticmethod
    def _explain_legend():
        # Keep tight, human-friendly, one-time legend for detail mode.
        print(f"{Fore.CYAN}Explain legend{Style.RESET_ALL}")
        print("  score parts: b=base, rg=regime, ed=edge, hv=HV-rank slot, cv=curvature, pen=penalties")
        print("  risk  parts: b=base, dte=time-to-expiry, gm=gamma proxy, cv=curvature risk, rg=regime risk, pen=penalties\n")

    # -------------------- detail --------------------

    def _detail_lines(self, title: str, rows: List[ScanRow], limit: int = 15):
        if not rows:
            return

        print(f"{Fore.CYAN}{title} details (per-row explain){Style.RESET_ALL}")
        self._explain_legend()

        squeeze_thr = self.cfg.get("risk", {}).get("squeeze_threshold", None)

        for r in rows[:limit]:
            # ----- score breakdown -----
            bd = r.score_breakdown
            pen = getattr(bd, "penalties", 0)
            pen_str = f" pen{pen:+d}" if pen else ""

            print(
                f"  {r.symbol:<6} score={r.cal_score:<3d} "
                f"[b{bd.base:+d} rg{bd.regime:+d} ed{bd.edge:+d} "
                f"hv{bd.hv:+d} cv{bd.curvature:+d}{pen_str}] "
                f"| edge={self._fmt_float(r.edge)}x tag={r.tag} hv={r.hv_rank:.0f}%"
            )

            # ----- risk breakdown -----
            rbd = getattr(r, "risk_breakdown", None)
            if rbd is None:
                print(f"         risk={r.short_risk:<3d} | short={r.short_exp} d{r.short_dte}")
                continue

            r_pen = getattr(rbd, "penalties", 0)
            r_pen_str = f" pen{r_pen:+d}" if r_pen else ""

            squeeze = getattr(r, "squeeze_ratio", None)
            squeeze_str = (
                f" | squeeze={self._fmt_float(squeeze,2)}x"
                if isinstance(squeeze, (int, float))
                else ""
            )

            print(
                f"         risk={r.short_risk:<3d} "
                f"[b{rbd.base:+d} "
                f"dte{getattr(rbd,'dte',0):+d} "
                f"gm{getattr(rbd,'gamma',0):+d} "
                f"cv{getattr(rbd,'curv',0):+d} "
                f"rg{getattr(rbd,'regime',0):+d}{r_pen_str}] "
                f"| short={r.short_exp} d{r.short_dte}{squeeze_str}"
            )

            # ----- curvature explain (human-friendly) -----
            if "S" in r.tag and getattr(rbd, "curv", 0) == 0:
                thr_str = str(squeeze_thr) if squeeze_thr is not None else "N/A"
                sq_str = self._fmt_float(squeeze, 2) if isinstance(squeeze, (int, float)) else "N/A"
                print(
                    f"         note: SPIKY_FRONT tag detected, but curvature risk not added "
                    f"(squeeze={sq_str}x, threshold={thr_str})"
                )

        print("")

    # -------------------- main render --------------------

    def render(
        self,
        days: int,
        universe_size: int,
        min_score: int,
        max_risk: int,
        strict: List[ScanRow],
        auto_adjusted: List[ScanRow],
        watch: List[ScanRow],
        top: List[ScanRow],
        errors: List[Tuple[str, str]],
        detail: bool
    ):
        print(f"\n{Fore.CYAN}{'='*95}")
        print(f"🧠 TRADE GUARDIAN :: SCANLIST (days={days})")
        print(f"{'='*95}{Style.RESET_ALL}")

        ranks = self.policy.probe_ranks()
        probe_str = f"{ranks[0]}..{ranks[-1]}" if ranks else "-"
        print(
            f"Short leg policy: base_rank={self.policy.base_rank}, "
            f"min_dte={self.policy.min_dte}, probe_ranks={probe_str}"
        )
        print(
            f"Universe size: {universe_size} | "
            f"Strict: {len(strict)} | AutoAdjusted: {len(auto_adjusted)} | "
            f"Watch: {len(watch)} | Errors: {len(errors)}"
        )
        print(f"Strict Filter: cal_score >= {min_score}, short_risk <= {max_risk}")
        print(
            f"Throttle: {self.cfg['scan']['throttle_sec']:.2f}s/ticker | "
            f"HV cache: {os.path.relpath(self.hv_cache_path)}\n"
        )

        print(f"{Fore.CYAN}🏆 Top Overall (ranked by score + edge + lower risk){Style.RESET_ALL}")
        top_rows = [[
            r.symbol, f"{r.price:.2f}", r.short_exp, r.short_dte,
            f"{r.short_iv:.1f}%", f"{r.base_iv:.1f}%",
            f"{r.edge:.2f}x", f"{r.hv_rank:.0f}%",
            r.cal_score, r.short_risk, r.tag
        ] for r in top]
        self._tab(
            ["Sym","Px","ShortExp","ShortDTE","ShortIV","BaseIV","Edge","HV%","Score","Risk","Tag"],
            top_rows[:15]
        )

        if detail:
            self._detail_lines("Top", top)

        self._tag_legend()

        if errors:
            print(Fore.YELLOW + "Errors (first 15):" + Style.RESET_ALL)
            for sym, msg in errors[:15]:
                print(f"  - {sym}: {msg}")
            print("")

    # -------------------- diagnostics --------------------

    def render_diagnostics(
        self,
        rows: List[ScanRow],
        min_score: int,
        max_risk: int,
        strict: List[ScanRow],
        auto_adjusted: List[ScanRow],
        detail: bool
    ):
        if not rows:
            return

        avg_score = float(np.mean([r.cal_score for r in rows]))
        avg_risk = float(np.mean([r.short_risk for r in rows]))
        avg_dte = float(np.mean([r.short_dte for r in rows]))
        avg_edge = float(np.mean([r.edge for r in rows]))

        print(f"{Fore.YELLOW}🧾 Diagnostics{Style.RESET_ALL}")
        print(
            f"   • Avg CalScore: {avg_score:.1f} | "
            f"Avg ShortRisk: {avg_risk:.1f} | "
            f"Avg ShortDTE: {avg_dte:.1f} | "
            f"Avg Edge(S/B): {avg_edge:.2f}x"
        )

        # ---- score breakdown avg ----
        bd_list = [r.score_breakdown for r in rows]
        print(
            f"   • Score avg breakdown: "
            f"base {np.mean([b.base for b in bd_list]):+.1f} | "
            f"regime {np.mean([b.regime for b in bd_list]):+.1f} | "
            f"edge {np.mean([b.edge for b in bd_list]):+.1f} | "
            f"hv {np.mean([b.hv for b in bd_list]):+.1f} | "
            f"curv {np.mean([b.curvature for b in bd_list]):+.1f}"
        )

        # ---- risk breakdown avg ----
        rbd_list = [getattr(r, "risk_breakdown", None) for r in rows]
        rbd_list = [x for x in rbd_list if x is not None]
        if rbd_list:
            print(
                f"   • Risk avg breakdown:  "
                f"base {np.mean([x.base for x in rbd_list]):+.1f} | "
                f"dte {np.mean([getattr(x,'dte',0) for x in rbd_list]):+.1f} | "
                f"gamma {np.mean([getattr(x,'gamma',0) for x in rbd_list]):+.1f} | "
                f"curv {np.mean([getattr(x,'curv',0) for x in rbd_list]):+.1f} | "
                f"regime {np.mean([getattr(x,'regime',0) for x in rbd_list]):+.1f}"
            )

        print("")
