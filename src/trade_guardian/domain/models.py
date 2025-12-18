from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HVInfo:
    status: str = "Error"
    current_hv: float = 0.0
    hv_rank: float = 0.0
    hv_low: float = 0.0
    hv_high: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    msg: str = ""


@dataclass
class TermPoint:
    exp: str
    dte: int
    strike: float
    mark: float
    iv: float
    delta: float = 0.0
    theta: float = 0.0
    gamma: float = 0.0


@dataclass
class TSFeatures:
    status: str
    msg: str = ""
    regime: str = "FLAT"          # CONTANGO / BACKWARDATION / FLAT
    curvature: str = "NORMAL"     # SPIKY_FRONT / NORMAL
    short_exp: str = ""
    short_dte: int = 0
    short_iv: float = 0.0
    base_iv: float = 0.0
    edge: float = 0.0            # short/base ratio
    squeeze_ratio: float = 0.0   # rank0/base ratio (front spike measure)


@dataclass
class ScoreBreakdown:
    base: int = 0
    regime: int = 0
    edge: int = 0
    hv: int = 0
    curvature: int = 0
    penalties: int = 0


@dataclass
class RiskBreakdown:
    """
    Explainable risk decomposition for the chosen short leg.

    Convention: each component is an integer "risk points".
    Total risk = base + dte + gamma + curvature + regime + penalties
    (Future slots: liquidity/event can be added without breaking interfaces.)
    """
    base: int = 0
    dte: int = 0
    gamma: int = 0
    curvature: int = 0
    regime: int = 0
    penalties: int = 0


@dataclass
class Recommendation:
    rec_rank: int
    rec_exp: str
    rec_dte: int
    rec_iv: float
    rec_edge: float
    rec_score: int
    rec_risk: int
    rec_tag: str
    rec_breakdown: ScoreBreakdown
    # ✅ default keeps backward compatibility for older recommend() code
    rec_risk_breakdown: RiskBreakdown = field(default_factory=RiskBreakdown)


@dataclass
class ScanRow:
    symbol: str
    price: float

    # short leg chosen by policy (base rank)
    short_exp: str
    short_dte: int
    short_iv: float

    # baseline (30-90)
    base_iv: float
    edge: float

    hv_rank: float

    regime: str
    curvature: str
    tag: str

    cal_score: int
    short_risk: int
    score_breakdown: ScoreBreakdown

    # ✅ default keeps backward compatibility for existing strategy.evaluate()
    risk_breakdown: RiskBreakdown = field(default_factory=RiskBreakdown)

    rec: Optional[Recommendation] = None
    probe_summary: str = ""
    
    # [新增] 存放生成的交易蓝图
    blueprint: Any = None # 实际上是 Optional[CalendarBlueprint]


@dataclass
class Context:
    symbol: str
    price: float
    vix: float
    term: List[TermPoint]
    hv: HVInfo
    tsf: Dict[str, Any]
    # [新增] 保留原始 Chain 数据，供 Blueprint 查找报价
    raw_chain: Dict[str, Any] = field(default_factory=dict)