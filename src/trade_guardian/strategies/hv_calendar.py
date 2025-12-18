from __future__ import annotations

from typing import Optional, Tuple

from trade_guardian.domain.models import Context, Recommendation, ScanRow
from trade_guardian.domain.policy import ShortLegPolicy
from trade_guardian.domain.scoring import Scoring, ScoringRules

# 复用 calendar 的风险连续模型 + explain（risk_breakdown）
from trade_guardian.strategies.calendar import CalendarStrategy


class HVCalendarStrategy(CalendarStrategy):
    """
    Strategy #2: HV-aware calendar
      - score: calendar score + hv adjust (写入 score_breakdown.hv)
      - risk : 复用 CalendarStrategy._risk_score（你当前版本需要 gamma/squeeze 等输入）
    """
    name = "hv_calendar"

    def __init__(self, cfg: dict, policy: ShortLegPolicy):
        super().__init__(cfg, policy)

        rules = ScoringRules(min_edge_short_base=float(cfg["rules"]["min_edge_short_base"]))
        self.scoring = Scoring(rules)

        hv_cfg = ((cfg.get("strategies", {}) or {}).get("hv_calendar", {}) or {})
        hv_rules = (hv_cfg.get("hv_rules", {}) or {})

        self.hv_low_rank = float(hv_rules.get("hv_low_rank", 20.0))
        self.hv_mid_rank = float(hv_rules.get("hv_mid_rank", 50.0))
        self.hv_high_rank = float(hv_rules.get("hv_high_rank", 70.0))

        self.hv_low_bonus = int(hv_rules.get("hv_low_bonus", 10))
        self.hv_mid_bonus = int(hv_rules.get("hv_mid_bonus", 4))
        self.hv_high_penalty = int(hv_rules.get("hv_high_penalty", -4))
        self.hv_extreme_penalty = int(hv_rules.get("hv_extreme_penalty", -10))

    def _hv_adjust(self, hv_rank: float) -> int:
        if hv_rank <= self.hv_low_rank:
            return self.hv_low_bonus
        if hv_rank <= self.hv_mid_rank:
            return self.hv_mid_bonus
        if hv_rank <= self.hv_high_rank:
            return 0
        if hv_rank <= 90.0:
            return self.hv_high_penalty
        return self.hv_extreme_penalty

    @staticmethod
    def _find_point_gamma(ctx: Context, exp: str, dte: int) -> float:
        """
        Best-effort: 在 ctx.term 里按 exp+dte 找对应点的 gamma。
        找不到就退化为 0.0（风险模型仍可跑，只是 gamma 分项会偏小/为 0）。
        """
        for p in ctx.term:
            if str(p.exp) == str(exp) and int(p.dte) == int(dte):
                try:
                    return float(getattr(p, "gamma", 0.0) or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    @staticmethod
    def _best_effort_squeeze_ratio(ctx: Context) -> float:
        """
        squeeze_ratio 在你项目里可能：
          - 已经由 TSFeatureBuilder 写入 ctx.tsf["squeeze_ratio"]
          - 或者没有（早先输出曾出现 N/A）
        这里统一返回 float，缺失就 0.0。
        """
        try:
            v = ctx.tsf.get("squeeze_ratio", 0.0)  # type: ignore
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    def evaluate(self, ctx: Context) -> ScanRow:
        tsf = ctx.tsf
        regime = str(tsf["regime"])
        curvature = str(tsf["curvature"])
        short_exp = str(tsf["short_exp"])
        short_dte = int(tsf["short_dte"])
        short_iv = float(tsf["short_iv"])
        base_iv = float(tsf["base_iv"])
        edge = float(tsf["edge"])
        hv_rank = float(ctx.hv.hv_rank)

        # ---------------- score (HV-aware) ----------------
        score, bd = self.scoring.score_calendar(
            regime=regime,
            curvature=curvature,
            edge=edge,
            hv_rank=hv_rank,
        )
        hv_adj = self._hv_adjust(hv_rank)
        bd.hv = int(hv_adj)
        score = int(score + hv_adj)

        # ---------------- risk (reuse calendar continuous model) ----------------
        eligible = [p for p in ctx.term if int(p.dte) >= int(self.policy.min_dte)]
        rank0_dte = int(eligible[0].dte) if eligible else int(short_dte)
        rank0_gamma = float(getattr(eligible[0], "gamma", 0.0) or 0.0) if eligible else 0.0

        short_gamma = self._find_point_gamma(ctx, exp=short_exp, dte=short_dte)
        squeeze_ratio = self._best_effort_squeeze_ratio(ctx)

        # ✅ 关键：严格按你当前 calendar.py 的 keyword-only 参数签名调用
        risk, rbd = self._risk_score(
            ctx,
            short_dte=short_dte,
            short_gamma=short_gamma,
            rank0_dte=rank0_dte,
            rank0_gamma=rank0_gamma,
            regime=regime,
            curvature=curvature,
            squeeze_ratio=squeeze_ratio,
        )

        tag = self._tag(regime, curvature)

        row = ScanRow(
            symbol=ctx.symbol,
            price=float(ctx.price),
            short_exp=short_exp,
            short_dte=short_dte,
            short_iv=short_iv,
            base_iv=base_iv,
            edge=edge,
            hv_rank=hv_rank,
            regime=regime,
            curvature=curvature,
            tag=tag,
            cal_score=int(score),
            short_risk=int(risk),
            score_breakdown=bd,
        )

        # renderer 用 getattr，所以我们用 setattr 挂上 explain 字段
        setattr(row, "risk_breakdown", rbd)
        setattr(row, "squeeze_ratio", squeeze_ratio)

        return row

    def recommend(self, ctx: Context, min_score: int, max_risk: int) -> Tuple[Optional[Recommendation], str]:
        ranks = self.policy.probe_ranks()
        eligible = [p for p in ctx.term if int(p.dte) >= int(self.policy.min_dte)]
        if not eligible:
            return None, "-"

        hv_rank = float(ctx.hv.hv_rank)
        hv_adj = self._hv_adjust(hv_rank)
        squeeze_ratio = self._best_effort_squeeze_ratio(ctx)

        rank0_dte = int(eligible[0].dte)
        rank0_gamma = float(getattr(eligible[0], "gamma", 0.0) or 0.0)

        best: Optional[Recommendation] = None
        best_summary = "-"

        for rk in ranks:
            if rk < 0 or rk >= len(eligible):
                continue

            p = eligible[rk]

            base_iv = float(ctx.tsf["base_iv"])
            edge = (float(p.iv) / base_iv) if base_iv > 0 else 0.0

            regime = str(ctx.tsf["regime"])
            curvature = str(ctx.tsf["curvature"])

            score, bd = self.scoring.score_calendar(regime=regime, curvature=curvature, edge=edge, hv_rank=hv_rank)
            bd.hv = int(hv_adj)
            score = int(score + hv_adj)

            short_gamma = float(getattr(p, "gamma", 0.0) or 0.0)

            risk, rbd = self._risk_score(
                ctx,
                short_dte=int(p.dte),
                short_gamma=short_gamma,
                rank0_dte=rank0_dte,
                rank0_gamma=rank0_gamma,
                regime=regime,
                curvature=curvature,
                squeeze_ratio=squeeze_ratio,
            )

            tag = self._tag(regime, curvature)

            if score >= min_score and risk <= max_risk:
                rec = Recommendation(
                    rec_rank=int(rk),
                    rec_exp=str(p.exp),
                    rec_dte=int(p.dte),
                    rec_iv=float(p.iv),
                    rec_edge=float(edge),
                    rec_score=int(score),
                    rec_risk=int(risk),
                    rec_tag=tag,
                    rec_breakdown=bd,
                )
                setattr(rec, "risk_breakdown", rbd)
                setattr(rec, "squeeze_ratio", squeeze_ratio)

                summary = f"ok rk{rk} {p.exp} d{p.dte} e{edge:.2f} s{score} r{risk} {tag}"
                return rec, summary

            if best is None or score > best.rec_score:
                best = Recommendation(
                    rec_rank=int(rk),
                    rec_exp=str(p.exp),
                    rec_dte=int(p.dte),
                    rec_iv=float(p.iv),
                    rec_edge=float(edge),
                    rec_score=int(score),
                    rec_risk=int(risk),
                    rec_tag=tag,
                    rec_breakdown=bd,
                )
                setattr(best, "risk_breakdown", rbd)
                setattr(best, "squeeze_ratio", squeeze_ratio)
                best_summary = f"best rk{rk} {p.exp} d{p.dte} e{edge:.2f} s{score} r{risk} {tag}"

        return None, best_summary
