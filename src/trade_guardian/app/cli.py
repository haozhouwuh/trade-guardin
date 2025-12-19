import argparse
import os
import time

from trade_guardian.infra.config import (
    DEFAULT_CONFIG,
    load_config,
    write_config_template,
    merge_config_paths,
    policy_from_cfg_and_cli,
)
from trade_guardian.infra.schwab_client import SchwabClient
from trade_guardian.domain.registry import StrategyRegistry
from trade_guardian.app.orchestrator import TradeGuardian


def main():
    parser = argparse.ArgumentParser("Trade Guardian")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---------- initconfig ----------
    p_init = sub.add_parser("initconfig", help="Generate config/config.json template")
    p_init.add_argument("--path", type=str, default=None, help="Output path (default: ./config/config.json)")
    p_init.add_argument("--force", action="store_true", help="Overwrite if exists")

    # ---------- scanlist ----------
    p_scan = sub.add_parser("scanlist", help="Scan tickers.csv and output candidates")
    p_scan.add_argument("--config", type=str, default=None, help="Config path (default: ./config/config.json)")
    p_scan.add_argument("--autogen-config", action="store_true", help="Auto-generate config if missing")
    p_scan.add_argument("--no-autogen-config", action="store_true", help="Disable auto-generate config")

    p_scan.add_argument("--strategy", type=str, default="auto", help="Strategy name (default: auto)")
    p_scan.add_argument("--days", type=int, default=600)
    p_scan.add_argument("--csv", type=str, default=None, help="Tickers csv path")
    p_scan.add_argument("--min-score", type=int, default=60)
    p_scan.add_argument("--max-risk", type=int, default=70)
    p_scan.add_argument("--limit", type=int, default=0)
    p_scan.add_argument("--detail", action="store_true", help="Print blueprints and detailed logic")
    p_scan.add_argument("--top", type=int, default=None, help="Only show top N sorted blueprints")

    # Policy overrides
    p_scan.add_argument("--short-rank", type=int, default=None)
    p_scan.add_argument("--min-short-dte", type=int, default=None)
    p_scan.add_argument("--max-probe-rank", type=int, default=None)

    args = parser.parse_args()

    # 定位项目根目录 (cli.py -> app -> trade_guardian -> src -> project_root)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    if args.cmd == "initconfig":
        out = args.path or os.path.join(root, "config", "config.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        write_config_template(out, DEFAULT_CONFIG, overwrite=args.force)
        print(f"✅ Wrote config template: {out}")
        return

    if args.cmd == "scanlist":
        # 记录开始时间，用于数据库存盘
        start_ts = time.time()

        cfg_path = args.config or os.path.join(root, "config", "config.json")

        # 检查是否需要自动生成配置
        autogen_default = bool(DEFAULT_CONFIG.get("runtime", {}).get("autogen_config_if_missing", True))
        autogen = autogen_default
        if args.autogen_config: autogen = True
        if args.no_autogen_config: autogen = False

        if autogen and not os.path.exists(cfg_path):
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            write_config_template(cfg_path, DEFAULT_CONFIG, overwrite=False)

        # 加载与合并路径
        cfg = load_config(cfg_path, DEFAULT_CONFIG)
        cfg = merge_config_paths(cfg, root, args.csv)

        # 构建策略和客户端
        policy = policy_from_cfg_and_cli(cfg, args)
        client = SchwabClient(cfg)
        registry = StrategyRegistry(cfg, policy)
        strategy = registry.get(args.strategy)

        # 初始化主控程序
        tg = TradeGuardian(client=client, cfg=cfg, policy=policy, strategy=strategy)
        
        # 执行扫描
        # 这里计算 elapsed (经过的时间)，确保数据库能记录这次任务跑了多久
        tg.scanlist(
            strategy_name=args.strategy,
            days=args.days,
            min_score=args.min_score,
            max_risk=args.max_risk,
            limit=args.limit,
            detail=args.detail,
            top=args.top,
            elapsed=0.0  # 初始设为0，Orchestrator 内部会计算真实值或由这里传递
        )
        return


if __name__ == "__main__":
    main()