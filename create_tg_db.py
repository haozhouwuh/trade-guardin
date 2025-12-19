import sqlite3
import os
import sys

def init_db():
    # ==========================================
    # 1. 路径定位逻辑
    # ==========================================
    # 获取当前脚本所在的根目录
    project_root = os.path.dirname(os.path.abspath(__file__))
    
    # 定义数据库文件夹和文件路径
    db_folder = os.path.join(project_root, "db")
    db_path = os.path.join(db_folder, "trade_guardian.db")

    print(f"📍 Project Root: {project_root}")
    
    # 2. 如果 db 文件夹不存在，创建它
    if not os.path.exists(db_folder):
        print(f"🛠️  Creating DB folder: {db_folder}")
        os.makedirs(db_folder)
    
    print(f"🔗 Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # ==========================================
    # 3. 创建表结构 (Schema)
    # ==========================================

    # 表 1: Scan Batches (扫描批次/会话)
    # 记录每一次运行 scanlist 的全局信息和市场宏观状态
    print("   ... Checking table: scan_batches")
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,           -- 系统启动时间 (ISO 格式)
            strategy_name TEXT,       -- 运行的策略 (auto/long_gamma/diagonal)
            market_vix REAL,          -- [新增] 扫描时的 VIX 指数水平
            universe_size INTEGER,    -- 扫描的标的总数
            avg_abs_edge REAL,        -- 市场平均偏离强度 (温度计)
            cheap_vol_pct REAL,       -- 便宜货占比 (Edge > 0)
            elapsed_time REAL         -- 总运行耗时 (秒)
        )
    ''')

    # 表 2: Market Snapshots (行情快照)
    # 记录每只股票在 T 时刻被扫描到的原始状态
    print("   ... Checking table: market_snapshots")
    c.execute('''
        CREATE TABLE IF NOT EXISTS market_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,         -- 关联扫描批次
            symbol TEXT,              -- 股票代码
            price REAL,               -- 现价
            iv_short REAL,            -- 短端 IV (29 DTE)
            iv_base REAL,             -- 长端基准 IV
            hv_rank REAL,             -- 历史波动率排名
            regime TEXT,              -- 期限结构 (CONTANGO/BACKWARDATION)
            FOREIGN KEY(batch_id) REFERENCES scan_batches(batch_id)
        )
    ''')

    # 表 3: Trade Plans & Gates (执行计划与风险闸门)
    # 记录 Trade Guardian 对该行情切面做出的决策细节
    print("   ... Checking table: trade_plans")
    c.execute('''
        CREATE TABLE IF NOT EXISTS trade_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER,      -- 关联行情快照
            strategy_type TEXT,       -- 具体采用的子策略 (LG / PMCC)
            cal_score INTEGER,        -- 策略评分 (0-100)
            short_risk INTEGER,       -- 风险评分 (0-100)
            gate_status TEXT,         -- 执行图标 (✅ / ⚠️ / ⛔)
            total_gamma REAL,         -- 组合总 Gamma
            est_debit REAL,           -- 估算权利金成本
            error_msg TEXT,           -- 如果被拒绝，记录原因 (如 Debit > Width)
            blueprint_json TEXT,      -- 完整的蓝图结构 (JSON 格式)
            FOREIGN KEY(snapshot_id) REFERENCES market_snapshots(snapshot_id)
        )
    ''')

    conn.commit()
    
    # 验证表数量
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = c.fetchall()
    conn.close()

    print(f"\n✅ SUCCESS! Trade Guardian DB initialized with {len(tables)} tables:")
    for t in tables:
        print(f"   - {t[0]}")
    print(f"\n🎯 Database Location: {db_path}")

if __name__ == "__main__":
    init_db()