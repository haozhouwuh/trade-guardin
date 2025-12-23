import argparse
import os
import time
import json
import sqlite3

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
from trade_guardian.action.sniper import Sniper

def main():
    parser = argparse.ArgumentParser("Trade Guardian")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---------- initconfig ----------
    p_init = sub.add_parser("initconfig", help="Generate config/config.json template")
    #p_init.add_argument("--path", type=str, default=None, help="Output path (default: ./config/config.json)")
    p_init.add_argument("--path", type=str, default=None, help="Output path (default: ./config/config.yaml)")
    p_init.add_argument("--force", action="store_true", help="Overwrite if exists")

    # ---------- scanlist ----------
    p_scan = sub.add_parser("scanlist", help="Scan tickers.csv and output candidates")
    #p_scan.add_argument("--config", type=str, default=None, help="Config path (default: ./config/config.json)")
    p_scan.add_argument("--config", type=str, default=None, help="Config path (default: ./config/config.yaml)")
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

    # ---------- [NEW] fire (智能开火) ----------
    p_fire = sub.add_parser("fire", help="Execute tactical lock & order generation")
    p_fire.add_argument("symbol", type=str, help="Target Symbol (e.g. NVDA)")
    p_fire.add_argument("--strategy", type=str, default=None, help="Strategy type (Optional, auto-load from DB)")
    
    # 短腿/主腿参数 (可选，不填则查库)
    p_fire.add_argument("--date", type=str, default=None, help="Short Expiry (YYYY-MM-DD)")
    p_fire.add_argument("--strike", type=float, default=None, help="Short Strike")
    
    # 长腿参数 (仅 Diagonal 需要，可选，不填则查库)
    p_fire.add_argument("--long-date", type=str, default=None, help="Long Expiry (YYYY-MM-DD)")
    p_fire.add_argument("--long-strike", type=float, default=None, help="Long Strike")

    # 定价急迫度
    p_fire.add_argument("--mode", type=str, default="PASSIVE", choices=["PASSIVE", "NEUTRAL", "AGGRESSIVE"], help="Pricing urgency")
    
    args = parser.parse_args()

    # 定位项目根目录 (cli.py -> app -> trade_guardian -> src -> project_root)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    # ==========================================
    # 1. initconfig
    # ==========================================
    if args.cmd == "initconfig":
        #out = args.path or os.path.join(root, "config", "config.json")
        out = args.path or os.path.join(root, "config", "config.yaml")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        write_config_template(out, DEFAULT_CONFIG, overwrite=args.force)
        print(f"✅ Wrote config template: {out}")
        return

    # ==========================================
    # 2. scanlist
    # ==========================================
    if args.cmd == "scanlist":
        # 记录开始时间，用于数据库存盘
        start_ts = time.time()

        #cfg_path = args.config or os.path.join(root, "config", "config.json")
        cfg_path = args.config or os.path.join(root, "config", "config.yaml")

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

    # ==========================================
    # 3. fire (智能执行)
    # ==========================================
    if args.cmd == "fire":
        symbol = args.symbol.upper()
        print(f"🔥 Guardian Sniper System Activated: {symbol}")
        
        # A. 初始化基础设施
        cfg_path = os.path.join(root, "config", "config.json")
        cfg = load_config(cfg_path, DEFAULT_CONFIG)
        client = SchwabClient(cfg)
        sniper = Sniper(client)
        
        # B. 参数解析与智能查库
        target_strategy = args.strategy
        short_exp = args.date
        short_strike = args.strike
        long_exp = args.long_date
        long_strike = args.long_strike

        # 如果没有提供关键参数，尝试从数据库获取“最新作战计划”
        if not short_exp:
            print(f"   ... Fetching latest strategic blueprint for {symbol} from DB ...")
            try:
                db_path = os.path.join(root, "db", "trade_guardian.db")
                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    
                    # 查询该标的最近一次生成的、非 FORBID 的交易计划
                    query = """
                        SELECT p.strategy_type, p.blueprint_json, b.timestamp, p.tag
                        FROM trade_plans p
                        JOIN market_snapshots s ON p.snapshot_id = s.snapshot_id
                        JOIN scan_batches b ON s.batch_id = b.batch_id
                        WHERE s.symbol = ? AND p.gate_status != 'FORBID'
                        ORDER BY b.batch_id DESC
                        LIMIT 1
                    """
                    row = cursor.execute(query, (symbol,)).fetchone()
                    conn.close()
                    
                    if row:
                        db_strat, bp_json, ts, db_tag = row
                        print(f"   ✅ Found Plan from {ts}: {db_tag} ({db_strat})")
                        
                        if bp_json:
                            bp_data = json.loads(bp_json)
                            legs = bp_data.get("legs", [])
                            
                            # 确定策略类型
                            if not target_strategy:
                                if "DIAGONAL" in str(db_strat).upper() or "DIAGONAL" in str(db_tag).upper():
                                    target_strategy = "DIAGONAL"
                                else:
                                    target_strategy = "STRADDLE"

                            # 解析 Legs (智能识别买卖腿)
                            if target_strategy == "DIAGONAL":
                                # 假设对角线通常是买远卖近
                                for leg in legs:
                                    if leg['action'] == 'SELL':
                                        short_exp = leg['exp']
                                        short_strike = float(leg['strike'])
                                    elif leg['action'] == 'BUY':
                                        long_exp = leg['exp']
                                        long_strike = float(leg['strike'])
                            else:
                                # Straddle/LG: 两条腿都是 Buy，或者同 Exp
                                if legs:
                                    short_exp = legs[0]['exp']
                                    short_strike = float(legs[0]['strike'])
                            
                            print(f"   📖 Loaded Params: {target_strategy} | Short: {short_exp} {short_strike} | Long: {long_exp} {long_strike}")
                        else:
                            print("   ⚠️  Plan found but blueprint data is empty.")
                    else:
                        print(f"   ⚠️  No active plan found in DB for {symbol}. Will attempt fallback.")
                else:
                    print("   ⚠️  Database not found.")
            
            except Exception as e:
                print(f"   ❌ DB Lookup Error: {e}")

        # C. 兜底逻辑：如果查库失败，至少能跑个 Straddle
        if not short_exp:
            print("   ... Auto-scanning for nearest expiry (Fallback) ...")
            try:
                _, term, _ = client.scan_atm_term(symbol, days=14)
                if term:
                    valid = [p for p in term if p.dte >= 0] 
                    if valid:
                        short_exp = valid[0].exp
                        target_strategy = target_strategy or "STRADDLE"
                        print(f"   -> Auto-selected Short Expiry: {short_exp}")
            except Exception as e:
                print(f"   ❌ Fallback Scan Failed: {e}")

        if not short_exp:
            print("❌ Error: Could not determine expiry. Run 'scanlist' first or specify --date.")
            return

        # D. 锁定目标 (Call Sniper)
        # 如果没有指定 Strike，传 0.0 给 Sniper 让它自己 Recenter ATM
        final_short_strike = short_strike if short_strike else 0.0
        final_strategy = target_strategy if target_strategy else "STRADDLE"

        result = sniper.lock_target(
            symbol=symbol,
            strategy=final_strategy,
            short_exp=short_exp,
            short_strike=final_short_strike,
            long_exp=long_exp,
            long_strike=long_strike,
            urgency=args.mode.upper() # [NEW] 传入定价急迫度
        )
        
        # E. 输出结果
        if result['status'] == 'READY':
            print(f"\n✅ COMMAND READY: {result['symbol']}")
            if 'strike' in result:
                print(f"   STRIKE: {result['strike']}")
            print(f"   LIMIT : ${result['limit_price']}")
            print(f"   COST  : ${result['est_cost']:.2f}")
        else:
            print(f"\n⛔ ABORT: {result.get('msg', 'Unknown Error')}")
            
        return

if __name__ == "__main__":
    main()