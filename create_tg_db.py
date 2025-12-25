import sqlite3
import os
import sys
import argparse

def init_db(reset_mode=False):
    # ==========================================
    # 1. 路径定位逻辑
    # ==========================================
    project_root = os.path.dirname(os.path.abspath(__file__))
    db_folder = os.path.join(project_root, "db")
    db_path = os.path.join(db_folder, "trade_guardian.db")

    print(f"📍 Project Root: {project_root}")
    
    if not os.path.exists(db_folder):
        os.makedirs(db_folder)
    
    # ==========================================
    # 2. 连接数据库
    # ==========================================
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # ==========================================
    # 3. 彻底重构交易表 (如果存在则删除)
    # ==========================================
    # 注意：这里我们保留 snapshot 和 scan 历史，只重置交易部分
    if reset_mode:
        print("💥 Dropping TRADE tables...")
        c.execute("DROP TABLE IF EXISTS trade_legs")
        c.execute("DROP TABLE IF EXISTS active_trades")
        # 如果你想连扫描记录也删，解开下面两行
        # c.execute("DROP TABLE IF EXISTS trade_plans")
        # c.execute("DROP TABLE IF EXISTS market_snapshots")
        # c.execute("DROP TABLE IF EXISTS scan_batches")

    # ==========================================
    # 4. 创建表结构 (Schema V2 - Relational)
    # ==========================================

    # ... (前 3 张表 scan_batches, market_snapshots, trade_plans 保持不变，此处省略，代码里保留即可) ...
    # 为了完整性，我把它们简写在这里，你的文件里请保留原样
    c.execute('''CREATE TABLE IF NOT EXISTS scan_batches (batch_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, strategy_name TEXT, market_vix REAL, universe_size INTEGER, avg_abs_edge REAL, cheap_vol_pct REAL, elapsed_time REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS market_snapshots (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, symbol TEXT, price REAL, iv_short REAL, iv_base REAL, edge REAL, hv_rank REAL, regime TEXT, FOREIGN KEY(batch_id) REFERENCES scan_batches(batch_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS trade_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER, strategy_type TEXT, cal_score INTEGER, short_risk INTEGER, gate_status TEXT, total_gamma REAL, est_debit REAL, error_msg TEXT, blueprint_json TEXT, tag TEXT, FOREIGN KEY(snapshot_id) REFERENCES market_snapshots(snapshot_id))''')

    # --- NEW: 主交易表 (Portfolio View) ---
    print("   ... Checking table: active_trades (V2)")
    c.execute('''
        CREATE TABLE IF NOT EXISTS active_trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER,
            symbol TEXT,
            strategy TEXT,                -- e.g. DIAGONAL, IC
            status TEXT,                  -- WORKING, OPEN, CLOSED, PARTIAL
            created_at TEXT,
            updated_at TEXT,
            
            initial_cost REAL,            -- 初始总花费 (Debit为正, Credit为负)
            quantity INTEGER,
            
            total_pnl REAL,               -- 实时计算回填
            notes TEXT,
            tags TEXT
        )
    ''')

    # --- NEW: 腿部详情表 (Leg View) ---
    print("   ... Checking table: trade_legs (V2)")
    c.execute('''
        CREATE TABLE IF NOT EXISTS trade_legs (
            leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,             -- 关联主表
            
            leg_index INTEGER,            -- 0, 1, 2, 3 (用于排序)
            action TEXT,                  -- BUY / SELL
            ratio INTEGER,                -- e.g. 1
            
            exp_date TEXT,                -- YYYY-MM-DD
            strike REAL,
            op_type TEXT,                 -- CALL / PUT
            
            -- 价格追踪
            entry_price REAL,             -- 单腿开仓均价 (估算或实填)
            current_price REAL,           -- 最新市价 (Monitor回填)
            close_price REAL,             -- 平仓价格
            
            status TEXT,                  -- OPEN, CLOSED, ROLLED
            
            FOREIGN KEY(trade_id) REFERENCES active_trades(trade_id)
        )
    ''')

    conn.commit()
    
    # 验证
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_legs';")
    if c.fetchone():
        print("✅ Table 'trade_legs' created successfully.")
    
    conn.close()
    print(f"\n🎯 Database Ready: {db_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Drop trade tables to apply new schema")
    args = parser.parse_args()
    init_db(reset_mode=args.reset)