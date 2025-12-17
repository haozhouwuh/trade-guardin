import argparse
import os

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
    p_scan.add_argument("--autogen-config", action="store_true", help="Auto-generate config if missing (override config)")
    p_scan.add_argument("--no-autogen-config", action="store_true", help="Disable auto-generate config if missing")

    p_scan.add_argument("--strategy", type=str, default="calendar", help="Strategy name (default: calendar)")
    p_scan.add_argument("--days", type=int, default=600)
    p_scan.add_argument("--csv", type=str, default=None, help="Tickers csv (default: ./data/tickers.csv)")
    p_scan.add_argument("--min-score", type=int, default=60)
    p_scan.add_argument("--max-risk", type=int, default=70)
    p_scan.add_argument("--limit", type=int, default=0)
    p_scan.add_argument("--detail", action="store_true", help="Print per-row explain lines (no wider tables)")

    # short leg policy overrides (no globals)
    p_scan.add_argument("--short-rank", type=int, default=None, help="Base expiry rank on eligible expiries")
    p_scan.add_argument("--min-short-dte", type=int, default=None, help="Min DTE for short leg eligibility")
    p_scan.add_argument("--max-probe-rank", type=int, default=None, help="Probe rank count, e.g. 3 => base..base+2")

    args = parser.parse_args()

    # cli.py is under src/trade_guardian/app => go up to project root
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    if args.cmd == "initconfig":
        out = args.path or os.path.join(root, "config", "config.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        write_config_template(out, DEFAULT_CONFIG, overwrite=args.force)
        print(f"✅ Wrote config template: {out}")
        return

    if args.cmd == "scanlist":
        cfg_path = args.config or os.path.join(root, "config", "config.json")

        # auto-generate config if missing (config default can be overridden by CLI)
        autogen_default = bool(DEFAULT_CONFIG.get("runtime", {}).get("autogen_config_if_missing", True))
        autogen = autogen_default
        if args.autogen_config:
            autogen = True
        if args.no_autogen_config:
            autogen = False

        if autogen and not os.path.exists(cfg_path):
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            write_config_template(cfg_path, DEFAULT_CONFIG, overwrite=False)

        cfg = load_config(cfg_path, DEFAULT_CONFIG)
        cfg = merge_config_paths(cfg, root, args.csv)

        policy = policy_from_cfg_and_cli(cfg, args)

        client = SchwabClient(cfg)
        registry = StrategyRegistry(cfg, policy)
        strategy = registry.get(args.strategy)

        tg = TradeGuardian(client=client, cfg=cfg, policy=policy, strategy=strategy)
        tg.scanlist(
            days=args.days,
            min_score=args.min_score,
            max_risk=args.max_risk,
            limit=args.limit,
            detail=args.detail,
        )
        return


if __name__ == "__main__":
    main()
