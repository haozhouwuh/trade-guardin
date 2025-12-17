from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Dict

from trade_guardian.domain.policy import ShortLegPolicy


DEFAULT_CONFIG: Dict[str, Any] = {
    "runtime": {
        "autogen_config_if_missing": True
    },
    "paths": {
        "tickers_csv": "./data/tickers.csv",
        "cache_dir": "./cache",
    },
    "scan": {
        "contract_type": "CALL",
        "throttle_sec": 0.50,
        "days_default": 600,
    },
    "rules": {
        "min_edge_short_base": 1.05,
    },
    "policy": {
        "base_rank": 1,
        "min_dte": 3,
        "max_probe_rank": 3
    }
}


def write_config_template(path: str, template: dict, overwrite: bool = False) -> None:
    if os.path.exists(path) and not overwrite:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)


def load_config(path: str, defaults: dict) -> dict:
    cfg = deepcopy(defaults)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = deep_merge(cfg, user_cfg)
    return cfg


def deep_merge(a: dict, b: dict) -> dict:
    out = deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def merge_config_paths(cfg: dict, project_root: str, csv_override: str | None) -> dict:
    out = deepcopy(cfg)
    tickers = csv_override or out["paths"]["tickers_csv"]
    out["paths"]["tickers_csv"] = abs_from_root(project_root, tickers)
    out["paths"]["cache_dir"] = abs_from_root(project_root, out["paths"]["cache_dir"])
    return out


def abs_from_root(root: str, p: str) -> str:
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(root, p))


def policy_from_cfg_and_cli(cfg: dict, args) -> ShortLegPolicy:
    base_rank = args.short_rank if args.short_rank is not None else int(cfg["policy"]["base_rank"])
    min_dte = args.min_short_dte if args.min_short_dte is not None else int(cfg["policy"]["min_dte"])
    max_probe_rank = args.max_probe_rank if args.max_probe_rank is not None else int(cfg["policy"]["max_probe_rank"])
    return ShortLegPolicy(base_rank=base_rank, min_dte=min_dte, max_probe_rank=max_probe_rank)
