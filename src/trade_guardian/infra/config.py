from __future__ import annotations

import json
import os
import yaml  # <--- [NEW] 引入 yaml
from typing import Any, Dict, Optional

from trade_guardian.domain.policy import ShortLegPolicy

# 默认配置保持字典结构不变 (代码里用)
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
        "lg_min_dte_etf": 7,
        "lg_min_dte_stock": 10,
        "pin_risk_threshold": 0.25
    },
    "policy": {
        "base_rank": 1,
        "min_dte": 3,
        "probe_count": 3,
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
    """
    [MOD] 支持加载 .yaml 或 .json 文件
    """
    if not os.path.exists(path):
        # 尝试找同名的 .yaml 文件 (如果传入的是 .json)
        base, ext = os.path.splitext(path)
        if ext == '.json':
            yaml_path = base + '.yaml'
            if os.path.exists(yaml_path):
                path = yaml_path
            else:
                return dict(default_cfg)
        else:
            return dict(default_cfg)

    with open(path, "r", encoding="utf-8") as f:
        # [MOD] 根据扩展名决定解析方式
        if path.endswith(('.yaml', '.yml')):
            user_cfg = yaml.safe_load(f)
        else:
            user_cfg = json.load(f)

    if not isinstance(user_cfg, dict):
        return dict(default_cfg)
    return _deep_merge(default_cfg, user_cfg)


def write_config_template(path: str, default_cfg: Dict[str, Any], overwrite: bool = False) -> None:
    """
    [MOD] 写入配置模板 (如果是 .yaml 则写入 YAML 格式)
    注意：程序自动写入时无法保留注释，建议用户手动维护 config.yaml
    """
    # 如果传入的是 .json 但我们想强制转为 yaml (可选)
    if path.endswith('.json'):
        path = path.replace('.json', '.yaml')

    if os.path.exists(path) and not overwrite:
        return
        
    os.makedirs(os.path.dirname(path), exist_ok=True)
    
    with open(path, "w", encoding="utf-8") as f:
        if path.endswith(('.yaml', '.yml')):
            # default_flow_style=False 保证输出为块状格式，更易读
            yaml.dump(default_cfg, f, default_flow_style=False, allow_unicode=True)
        else:
            json.dump(default_cfg, f, indent=2, ensure_ascii=False)

def merge_config_paths(cfg: Dict[str, Any], root: str, csv_override: Optional[str]) -> Dict[str, Any]:
    # ... (保持不变) ...
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

    if getattr(args, "short_rank", None) is not None:
        base_rank = int(args.short_rank)
    if getattr(args, "min_short_dte", None) is not None:
        min_dte = int(args.min_short_dte)

    if getattr(args, "max_probe_rank", None) is not None:
        mx = int(args.max_probe_rank)
        probe_count = max(1, (mx - int(base_rank) + 1))

    return ShortLegPolicy(int(base_rank), int(min_dte), int(probe_count))