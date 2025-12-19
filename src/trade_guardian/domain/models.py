from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

# --- 基础设施类 (用于 SchwabClient 等) ---

@dataclass
class HVInfo:
    """Historical Volatility Data Container"""
    current_hv: float = 0.0
    hv_rank: float = 0.0
    hv_percentile: float = 0.0
    high_52w: float = 0.0
    low_52w: float = 0.0
    # [Fix] 补全 SchwabClient 需要的状态字段
    status: str = "Success"
    msg: str = ""
    # [Optional] 如果还需要其他统计字段，可以在此保留
    hv_low: float = 0.0
    hv_high: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0

@dataclass
class TermPoint:
    """Term Structure Point (用于 term structure 计算)"""
    exp: str = "" # [Fix] 确保有默认值，对应 date_iso
    exp_date: str = "" # 兼容旧代码
    dte: int = 0
    iv: float = 0.0
    strike: float = 0.0
    mark: float = 0.0
    delta: float = 0.0
    theta: float = 0.0
    gamma: float = 0.0

# --- 核心分析类 ---

@dataclass
class IVData:
    rank: float = 0.0
    percentile: float = 0.0
    current_iv: float = 0.0
    hv_rank: float = 0.0
    current_hv: float = 0.0

@dataclass
class Context:
    symbol: str
    price: float
    iv: IVData
    hv: IVData
    tsf: dict  # Term Structure Factors
    raw_chain: dict
    metrics: Any = None # Optional for Greeks

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
    base: int = 0
    dte: int = 0
    gamma: int = 0
    regime: int = 0
    curvature: int = 0
    penalties: int = 0

@dataclass
class ScanRow:
    symbol: str
    price: float
    short_exp: str
    short_dte: int
    short_iv: float
    base_iv: float
    edge: float
    hv_rank: float
    regime: str
    curvature: str
    tag: str
    cal_score: int
    short_risk: int
    score_breakdown: ScoreBreakdown
    risk_breakdown: RiskBreakdown
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Recommendation:
    strategy: str
    symbol: str
    action: str
    rationale: str
    entry_price: float
    score: int
    conviction: str
    meta: dict

# --- 执行蓝图类 (Orchestrator 需要) ---

@dataclass
class OrderLeg:
    """定义期权策略的一条腿"""
    symbol: str
    action: str      # BUY / SELL
    ratio: int       # e.g. 1
    exp: str         # Expiry Date (YYYY-MM-DD)
    strike: float
    type: str        # CALL / PUT

@dataclass
class Blueprint:
    """定义最终生成的执行蓝图"""
    symbol: str
    strategy: str
    legs: List[OrderLeg] = field(default_factory=list)
    est_debit: float = 0.0
    note: str = ""
    gamma_exposure: float = 0.0
    error: Optional[str] = None