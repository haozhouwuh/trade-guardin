from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from trade_guardian.domain.policy import ShortLegPolicy

DEFAULT_CONFIG: Dict[str, Any] = {
    "paths": {
        "tickers_csv": "data/tickers.csv",
        "cache_dir": "cache",
    },
    "scan": {
        "throttle_sec": 0.50,
        "contract_type": "ALL",
    },
    "rules": {
        "min_edge_short_base": 1.05,
    },
    "policy": {
        "base_rank": 1,
        "min_dte": 3,
        # 兼容两种写法：probe_count / max_probe_rank
        # - probe_count=3 => 探测 base..base+2
        "probe_count": 3,
        # "max_probe_rank": 3,
    },
    "strategies": {
        "hv_calendar": {
            "hv_rules": {
                "hv_low_rank": 20.0,
                "hv_mid_rank": 50.0,
                "hv_high_rank": 70.0,
                "hv_low_bonus": 10,
                "hv_mid_bonus": 4,
                "hv_high_penalty": -4,
                "hv_extreme_penalty": -10,
            }
        }
    },
}


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def load_config(path: str, default_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(path):
        return dict(default_cfg)
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = json.load(f)
    if not isinstance(user_cfg, dict):
        return dict(default_cfg)
    return _deep_merge(default_cfg, user_cfg)


def write_config_template(path: str, default_cfg: Dict[str, Any], overwrite: bool = False) -> None:
    if os.path.exists(path) and not overwrite:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(default_cfg, f, indent=2, ensure_ascii=False)


def merge_config_paths(cfg: Dict[str, Any], root: str, csv_override: Optional[str]) -> Dict[str, Any]:
    out = dict(cfg)
    out.setdefault("paths", {})
    paths = dict(out["paths"])

    if csv_override:
        paths["tickers_csv"] = csv_override

    tickers_csv = paths.get("tickers_csv", "data/tickers.csv")
    if not os.path.isabs(tickers_csv):
        tickers_csv = os.path.join(root, tickers_csv)
    paths["tickers_csv"] = os.path.normpath(tickers_csv)

    cache_dir = paths.get("cache_dir", "cache")
    if not os.path.isabs(cache_dir):
        cache_dir = os.path.join(root, cache_dir)
    paths["cache_dir"] = os.path.normpath(cache_dir)

    out["paths"] = paths
    return out


def _resolve_probe_count(pcfg: Dict[str, Any], base_rank: int) -> int:
    """
    Return probe_count (>=1).
    支持：
      - policy.probe_count
      - policy.max_probe_rank (inclusive)
    """
    if "probe_count" in pcfg and pcfg.get("probe_count") is not None:
        try:
            c = int(pcfg["probe_count"])
            return max(1, c)
        except Exception:
            return 3

    if "max_probe_rank" in pcfg and pcfg.get("max_probe_rank") is not None:
        try:
            mx = int(pcfg["max_probe_rank"])
            # max_probe_rank is inclusive absolute rank; convert to count
            return max(1, (mx - int(base_rank) + 1))
        except Exception:
            return 3

    return 3


def policy_from_cfg_and_cli(cfg: Dict[str, Any], args) -> ShortLegPolicy:
    """
    Build ShortLegPolicy from config + CLI overrides.

    CLI args:
      --short-rank
      --min-short-dte
      --max-probe-rank   (meaning: absolute inclusive rank upper bound)
    """
    pcfg = (cfg.get("policy", {}) or {})

    base_rank = int(pcfg.get("base_rank", 1))
    min_dte = int(pcfg.get("min_dte", 3))
    probe_count = _resolve_probe_count(pcfg, base_rank)

    # CLI overrides
    if getattr(args, "short_rank", None) is not None:
        base_rank = int(args.short_rank)
    if getattr(args, "min_short_dte", None) is not None:
        min_dte = int(args.min_short_dte)

    if getattr(args, "max_probe_rank", None) is not None:
        mx = int(args.max_probe_rank)
        probe_count = max(1, (mx - int(base_rank) + 1))

    # ✅ 用位置参数：避免 dataclass 字段名变化导致的 keyword 崩溃
    # 约定：ShortLegPolicy(base_rank, min_dte, probe_count)
    return ShortLegPolicy(int(base_rank), int(min_dte), int(probe_count))
