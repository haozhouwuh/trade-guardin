import sqlite3
import os
from datetime import datetime

class PersistenceManager:
    def __init__(self, db_path="db/trade_guardian.db"):
        self.db_path = db_path
        # 数据库结构完全由用户的 create_tg_db.py 掌控，这里不再干涉
        if not os.path.exists(self.db_path):
             print(f"⚠️ Warning: DB not found at {self.db_path}. Please ensure create_tg_db.py has been run.")

    def save_scan_session(self, strategy_name, vix, count, _u1, _u2, _u3, results_pack):
        """保存单次扫描的所有结果"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        try:
            # 1. 创建批次
            # 对应 DB 字段: universe_size (不是 ticker_count)
            current_time = datetime.now().isoformat()
            
            c.execute("""INSERT INTO scan_batches 
                      (timestamp, strategy_name, market_vix, universe_size) 
                      VALUES (?, ?, ?, ?)""",
                      (current_time, strategy_name, vix, count))
            batch_id = c.lastrowid
            
            # 2. 插入详情
            for item in results_pack:
                row, ctx, bp, gate = item
                
                # 插入快照
                # 对应 DB 表: market_snapshots
                c.execute("""INSERT INTO market_snapshots 
                          (batch_id, symbol, price, iv_short, iv_base, edge) 
                          VALUES (?, ?, ?, ?, ?, ?)""",
                          (batch_id, row.symbol, row.price, row.short_iv, row.base_iv, row.edge))
                snap_id = c.lastrowid
                
                # 准备 Plan 数据
                # [models.py] ScanRow 类明确有 tag 字段，直接读取
                tag_val = row.tag 
                est_gamma = row.meta.get("est_gamma", 0.0)
                
                # 蓝图数据处理
                est_debit = 0.0
                strat_name = "NONE"
                
                if bp:
                    est_debit = bp.est_debit
                    # [models.py] Blueprint 类明确字段名为 strategy
                    strat_name = bp.strategy 
                
                # 插入计划
                # 对应 DB 表: trade_plans
                # 对应 DB 字段: strategy_type (这是建表时的列名), tag
                # 注意：trade_plans 表没有 symbol 字段 (通过 snapshot_id 关联)，不要强行写入
                c.execute("""INSERT INTO trade_plans 
                          (snapshot_id, strategy_type, cal_score, gate_status, est_debit, total_gamma, tag) 
                          VALUES (?, ?, ?, ?, ?, ?, ?)""",
                          (snap_id, strat_name, row.cal_score, gate, est_debit, est_gamma, tag_val))
                
            conn.commit()
            print(f"💾 [DB] Persistent Success. Batch ID: {batch_id}")
            
        except Exception as e:
            # 打印详细错误，不再掩盖
            import traceback
            traceback.print_exc()
            print(f"❌ [DB Error] Save failed: {e}")
        finally:
            conn.close()