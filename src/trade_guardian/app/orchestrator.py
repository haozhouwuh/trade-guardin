from __future__ import annotations

import os
import sys
import time
from datetime import datetime, date
from typing import List, Tuple, Optional, Any

import pandas as pd
from colorama import Fore, Style

from trade_guardian.domain.models import Context, ScanRow, Blueprint, OrderLeg, TermPoint
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

        # [FIX] 读取配置（用规则层阈值）
        self.micro_min = float(cfg.get("rules", {}).get("diag_micro_min", 0.08))
        self.month_min = float(cfg.get("rules", {}).get("diag_month_min", 0.15))
        self.gamma_soft = float(cfg.get("rules", {}).get("gamma_soft_cap", DEFAULT_GAMMA_SOFT))
        self.gamma_hard = float(cfg.get("rules", {}).get("gamma_hard_cap", DEFAULT_GAMMA_HARD))

        # LG Spread 风控（如果 config 里有）
        self.lg_max_spread_pct = cfg.get("rules", {}).get("lg_max_spread_pct", None)

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

    # -------------------------
    # Core Scan Loop
    # -------------------------
    def scanlist(
        self,
        strategy_name: str = "auto",
        days: int = 600,
        min_score: int = 60,
        max_risk: int = 70,
        detail: bool = False,
        limit: int = None,
        **kwargs,
    ):
        start_ts = time.time()

        try:
            vix_q = self.client.get_quote("$VIX")
            current_vix = float(vix_q.get("lastPrice") or 0.0)
        except Exception:
            current_vix = 0.0

        tickers = self._get_universe()
        if limit:
            tickers = tickers[:limit]

        db_results_pack = []
        strict_results = []
        current_rows_for_next_batch = []

        FMT = (
            "{sym:<5} {px:<7} {sexp:<11} {sdte:<3} {siv:>6} | "
            "{mexp:<11} {mdte:<3} {miv:>6} {em:>5} | "
            "{kexp:<11} {kdte:<3} {kiv:>6} {ek:>5} | "
            "{sc:>4} {shp:<8} {gate:<6}   {tag:<8}"
        )
        HEADER = FMT.format(
            sym="Sym",
            px="Px",
            sexp="ShortExp",
            sdte="D",
            siv="S_IV",
            mexp="MicroExp",
            mdte="D",
            miv="M_IV",
            em="EdgM",
            kexp="MonthExp",
            kdte="D",
            kiv="K_IV",
            ek="EdgK",
            sc="Scr",
            shp="Shape",
            gate="Gate",
            tag="Tag",
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

                # -------------------------
                # Momentum (Delta_15m-ish) without污染
                # -------------------------
                iv_diff = 0.0
                if self.last_batch_df is not None and not self.last_batch_df.empty:
                    if "short_exp" in self.last_batch_df.columns:
                        prev = self.last_batch_df[
                            (self.last_batch_df["symbol"] == row.symbol)
                            & (self.last_batch_df["short_exp"] == row.short_exp)
                        ]
                        if not prev.empty and "iv" in prev.columns:
                            try:
                                iv_diff = float(row.short_iv) - float(prev.iloc[0]["iv"])
                            except Exception:
                                iv_diff = 0.0

                mom_type = "QUIET"
                if iv_diff > 2.0:
                    mom_type = "PULSE"
                elif iv_diff > 0.5:
                    mom_type = "TREND"
                elif iv_diff < -1.0:
                    mom_type = "CRUSH"

                if row.meta is None:
                    row.meta = {}
                row.meta["delta_15m"] = iv_diff
                row.meta["momentum"] = mom_type

                # -------------------------
                # Blueprint resolve (then 方案A对齐)
                # -------------------------
                bp = getattr(row, "blueprint", None)
                if not bp:
                    bp = self.plan(ctx, row)
                    row.blueprint = bp

                # ✅ 方案A：DIAGONAL 时，表格的 MonthExp/EdgK 强制等于 blueprint long leg
                # （必须放在 shape/gate 之前）
                if bp:
                    self._sync_diag_meta_to_blueprint(ctx, row, bp)

                # -------------------------
                # Shape calc (use synchronized meta)
                # -------------------------
                tsf = ctx.tsf or {}
                regime = str(tsf.get("regime", "FLAT"))
                is_squeeze = bool(tsf.get("is_squeeze", False))

                em = float(row.meta.get("edge_micro", 0.0) or 0.0)
                ek = float(row.meta.get("edge_month", 0.0) or 0.0)

                shape = "FLAT"
                if regime == "BACKWARDATION":
                    shape = "BACKWARD"
                elif ek >= 0.20 and em < 0.08:
                    shape = "FFBS"
                elif is_squeeze or em >= 0.12:
                    shape = "SPIKE"
                elif ek >= 0.20:
                    shape = "STEEP"
                elif 0.15 <= ek < 0.20:
                    shape = "MILD"
                else:
                    shape = "FLAT"
                row.meta["shape"] = shape

                # -------------------------
                # Gate
                # -------------------------
                gate, reason = self._get_gate_status(row, bp, mom_type)

                # bp.error 只认结构性错误；Gate Reason 写入 Note 和 Meta
                if gate not in ("EXEC", "LIMIT"):
                    row.meta["gate_reason"] = reason
                    if bp and (not getattr(bp, "note", "")):
                        bp.note = reason

                db_results_pack.append((row, ctx, bp, gate))

                # 保存 short_exp 供下轮使用
                current_rows_for_next_batch.append(
                    {"symbol": row.symbol, "iv": row.short_iv, "short_exp": row.short_exp}
                )

                if gate != "FORBID":
                    strict_results.append((row, ctx, bp, gate, mom_type, reason))

                # -------------------------
                # Print row
                # -------------------------
                if gate == "EXEC":
                    g_color = Fore.GREEN
                elif gate == "LIMIT":
                    g_color = Fore.CYAN
                elif gate == "FORBID":
                    g_color = Fore.RED
                else:
                    g_color = Fore.YELLOW

                gate_display = f"{g_color}{gate:<6}{Style.RESET_ALL}"
                tag_str = str(row.tag) if row.tag else ""

                print(
                    FMT.format(
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
                        shp=shape,
                        gate=gate_display,
                        tag=tag_str,
                    )
                )

            except Exception as e:
                print(f"❌ CRASH on {ticker}: {e}")
                import traceback

                traceback.print_exc()
                continue

        if current_rows_for_next_batch:
            self.last_batch_df = pd.DataFrame(current_rows_for_next_batch)

        elapsed = time.time() - start_ts
        valid_rows = [item[0] for item in db_results_pack]
        avg_abs_edge = 0.0
        cheap_vol_pct = 0.0

        if valid_rows:
            total_abs_edge = sum(abs(float(r.edge)) for r in valid_rows)
            avg_abs_edge = total_abs_edge / len(valid_rows)
            cheap_count = sum(1 for r in valid_rows if float(r.edge) > 0)
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

    # -------------------------
    # Helpers (DTE / Term IV)
    # -------------------------
    def _dte_from_exp(self, exp: str) -> int:
        try:
            d = datetime.strptime(str(exp), "%Y-%m-%d").date()
            return max(0, (d - date.today()).days)
        except Exception:
            return 0

    def _term_iv_by_exp(self, ctx: Context, exp: str) -> float:
        """
        ctx.term: List[TermPoint]
        """
        try:
            for p in (getattr(ctx, "term", None) or []):
                if str(p.exp) == str(exp):
                    iv = float(p.iv or 0.0)
                    if 0 < iv < 1.5:
                        iv *= 100.0
                    return iv
        except Exception:
            pass
        return 0.0

    def _sync_diag_meta_to_blueprint(self, ctx: Context, row: ScanRow, bp: Blueprint) -> None:
        """
        方案A：让 row.meta 的 month_* 永远等于 blueprint 实际 long leg
        """
        try:
            if not bp or str(getattr(bp, "strategy", "")).upper() != "DIAGONAL":
                return
            legs = getattr(bp, "legs", None) or []
            if not legs:
                return

            # long leg：BUY
            long_leg = next((leg for leg in legs if str(getattr(leg, "action", "")).upper() == "BUY"), None)
            if not long_leg:
                return

            long_exp = str(long_leg.exp)
            long_dte = self._dte_from_exp(long_exp)
            long_iv = self._term_iv_by_exp(ctx, long_exp)

            if row.meta is None:
                row.meta = {}

            # 强制对齐：MonthExp/MonthDTE/K_IV
            row.meta["month_exp"] = long_exp
            row.meta["month_dte"] = long_dte
            if long_iv > 0:
                row.meta["month_iv"] = long_iv

            # 重新计算 edge_month（让 EdgK 和 K_IV 同步）
            short_iv = float(row.short_iv or 0.0)
            base_iv = float(row.meta.get("month_iv") or 0.0)
            IV_FLOOR = 12.0
            denom = max(IV_FLOOR, short_iv if short_iv > 0 else IV_FLOOR)
            row.meta["edge_month"] = (base_iv - short_iv) / denom
        except Exception:
            # 这里绝不抛异常，避免 scan loop 崩
            return

    # -------------------------
    # Gate Logic (ALWAYS returns tuple)
    # -------------------------
    def _get_gate_status(self, row: ScanRow, bp: Optional[Blueprint], dna_type: str) -> Tuple[str, str]:
        status = "WAIT"
        reason = "Score/Structure suboptimal"

        try:
            # 1) Hard Kill
            if not bp:
                return "FORBID", "No Blueprint"

            bp_error = getattr(bp, "error", None)
            if bp_error:
                return "FORBID", f"Blueprint Error: {bp_error}"

            est_gamma = float((row.meta or {}).get("est_gamma", 0.0) or 0.0)
            if est_gamma >= float(self.gamma_hard):
                return "FORBID", f"Gamma {est_gamma:.3f} >= {self.gamma_hard}"

            if dna_type == "CRUSH":
                return "FORBID", "Momentum: IV CRUSH (-Delta)"

            # 2) Strategy Logic
            tag = str(getattr(row, "tag", "") or "")
            em = float((row.meta or {}).get("edge_micro", 0.0) or 0.0)
            ek = float((row.meta or {}).get("edge_month", 0.0) or 0.0)
            shape = str((row.meta or {}).get("shape", "FLAT") or "FLAT")

            strat_type = str(
                getattr(row, "strategy_type", None) or getattr(bp, "strategy", "") or ""
            ).upper()

            # --- LG / STRADDLE ---
            if ("LG" in tag) or ("STRADDLE" in strat_type):
                # Spread Check
                max_spread = (row.meta or {}).get("max_spread_pct", None)
                if max_spread is None:
                    max_spread = self.lg_max_spread_pct

                bp_meta = getattr(bp, "meta", None) or {}
                spread_pct = bp_meta.get("spread_pct", None)

                if (max_spread is not None) and (spread_pct is not None):
                    try:
                        if float(spread_pct) > float(max_spread):
                            return "FORBID", f"Spread {float(spread_pct):.1%} > {float(max_spread):.1%}"
                    except Exception:
                        pass

                allowed_exec = self.cfg.get("rules", {}).get("lg_allowed_dna_exec", ["PULSE"])
                allowed_limit = self.cfg.get("rules", {}).get("lg_allowed_dna_limit", ["TREND"])

                if dna_type in allowed_exec:
                    status, reason = "EXEC", f"Momentum {dna_type} (Aggressive)"
                elif dna_type in allowed_limit:
                    status, reason = "LIMIT", f"Momentum {dna_type} (Passive)"
                else:
                    status, reason = "WAIT", "Market Sleeping (Theta Burn)"

                if em < self.micro_min and ek < self.month_min:
                    return "WAIT", "Edges Too Low"

            # --- DIAG ---
            elif "DIAG" in tag:
                if ek < self.month_min:
                    status, reason = "WAIT", f"Back Edge {ek:.2f} < {self.month_min}"
                elif shape in ("FFBS", "STEEP"):
                    status, reason = "EXEC", "Structure Prime"
                elif shape == "SPIKE":
                    status, reason = "WAIT", "Spike Shape (Front IV too high)"
                else:
                    if em < self.micro_min:
                        status, reason = "WAIT", f"Front Edge {em:.2f} too low"
                    else:
                        status, reason = "LIMIT", "Structure OK"

            # --- Vertical ---
            elif ("BULL" in tag) or ("BEAR" in tag) or ("VERT" in tag):
                if int(getattr(row, "cal_score", 0) or 0) >= 60:
                    status, reason = "LIMIT", "Vertical Setup OK"
                else:
                    status, reason = "WAIT", f"Score {int(row.cal_score)} < 60"

            # 3) Soft Cap
            if status in ("EXEC", "LIMIT") and est_gamma >= float(self.gamma_soft):
                status, reason = "LIMIT", f"Gamma {est_gamma:.3f} > {self.gamma_soft} (Soft Cap)"

            # 4) General Momentum
            if status == "EXEC" and "LG" not in tag:
                if dna_type == "QUIET":
                    status, reason = "LIMIT", "Momentum Quiet (Wait for Pulse)"

            # 5) Diagnostic
            if row.symbol in LEV_ETFS and est_gamma > 0.20:
                reason += " [LEV_ETF Risk]"

            return status, reason

        except Exception as e:
            # 兜底：任何异常都不允许返回 None
            return "WAIT", f"GateError: {e}"

    # -------------------------
    # Fallback planner
    # -------------------------
    def plan(self, ctx: Context, row: ScanRow) -> Blueprint:
        """
        当策略没产出 blueprint 时的兜底：用 short_exp 做 STRADDLE。
        注意：永远返回 Blueprint，不返回 None。
        """
        bp = build_straddle_blueprint(symbol=ctx.symbol, underlying=ctx.price, chain=ctx.raw_chain, exp=row.short_exp)
        if bp:
            if not getattr(bp, "note", ""):
                bp.note = "Fallback Gamma Plan"
            return bp

        # 最终兜底（保证 Blueprint 构造不依赖位置参数）
        return Blueprint(symbol=ctx.symbol, strategy="STRADDLE", legs=[], est_debit=0.0, note="Build Failed", error="No Pricing Data")

    # -------------------------
    # Pretty print
    # -------------------------
    def _print_enhanced_blueprint(self, bp: Blueprint, row: ScanRow, dna: str, gate: str, reason: str):
        tactic = ""
        if gate == "LIMIT":
            tactic = f"{Fore.CYAN}[挂单潜伏] Limit @ Mid-$0.05 | {reason}{Style.RESET_ALL}"
        elif gate == "EXEC":
            tactic = f"{Fore.GREEN}[立即执行] Market/Mid+$0.02 | {reason}{Style.RESET_ALL}"
        elif gate == "WAIT":
            tactic = f"{Fore.YELLOW}[保持关注] {reason}{Style.RESET_ALL}"
        else:
            tactic = f"{Fore.RED}[禁止] {reason}{Style.RESET_ALL}"

        strat_name = getattr(bp, "strategy", None) or "UNKNOWN"
        print(
            f" {Fore.WHITE}{bp.symbol:<5} {strat_name:<13} | Gate: {gate:<5} | Debit: ${bp.est_debit} | Gamma: {(row.meta or {}).get('est_gamma', 0):.4f}"
        )
        print(f"    Edges: Micro {(row.meta or {}).get('edge_micro', 0):.2f} / Month {(row.meta or {}).get('edge_month', 0):.2f}")

        shape = (row.meta or {}).get("shape", "")
        mom = (row.meta or {}).get("momentum", "QUIET")
        print(f"    Shape: {shape:<8} | Momentum: {mom}")

        if "DIAG" in str(getattr(row, "tag", "") or "") and shape == "FFBS":
            print(f"    ✅ {Fore.GREEN}FFBS (Front-Flat Back-Steep): 完美对角线形态{Style.RESET_ALL}")

        print(f"    👉 {tactic}")

        legs = getattr(bp, "legs", None) or []
        if legs:
            for leg in legs:
                action = str(getattr(leg, "action", "")).upper()
                action_sym = "+" if action == "BUY" else "-"
                print(f"       {action_sym}{leg.ratio} {leg.exp} {leg.strike:<6} {leg.type}")
        else:
            print(f"       [ERROR] No Legs: {getattr(bp, 'error', None)}")
        print(f"    {'=' * 80}")

    # -------------------------
    # Strategy loader
    # -------------------------
    def _load_strategy(self, name: str):
        from trade_guardian.domain.registry import StrategyRegistry

        registry = StrategyRegistry(self.cfg, self.policy)
        try:
            return registry.get(name)
        except Exception:
            from trade_guardian.strategies.auto import AutoStrategy

            return AutoStrategy(self.cfg, self.policy)
