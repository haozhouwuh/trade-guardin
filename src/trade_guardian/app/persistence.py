import sqlite3
import os
from datetime import datetime

class PersistenceManager:
    def __init__(self, db_path=None):
        # [FIX] (C) 路径锚定：无论在哪里运行，都定位到项目根目录下的 db 文件夹
        if db_path:
            self.db_path = db_path
        else:
            # 当前文件在 src/trade_guardian/app/
            base_dir = os.path.dirname(os.path.abspath(__file__))
            # 回退 3 层到项目根目录 (src/trade_guardian/app -> src/trade_guardian -> src -> root)
            project_root = os.path.abspath(os.path.join(base_dir, "..", "..", ".."))
            self.db_path = os.path.join(project_root, "db", "trade_guardian.db")

        # 确保存放目录存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)


    # [FIX] Issue B: 接收统计参数 (avg_edge, cheap_vol, elapsed)
    def save_scan_session(self, strategy_name, vix, count, avg_edge, cheap_vol, elapsed, results_pack):
        """保存单次扫描的所有结果"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        try:
            # [FIX] (B) 时间格式修复：SQLite 对 ISO 8601 (带T) 支持不好，改用空格分隔
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute("""INSERT INTO scan_batches 
                      (timestamp, strategy_name, market_vix, universe_size, avg_abs_edge, cheap_vol_pct, elapsed_time) 
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (current_time, strategy_name, vix, count, avg_edge, cheap_vol, elapsed))
            batch_id = c.lastrowid
            
            for item in results_pack:
                row, ctx, bp, gate = item
                
                # [FIX] (D) 补全字段：写入 hv_rank 和 regime，防止数据丢失
                # 注意：这里需要 create_tg_db.py 里 market_snapshots 表结构配合 (你之前的 schema 已经有了)
                c.execute("""INSERT INTO market_snapshots 
                          (batch_id, symbol, price, iv_short, iv_base, edge, hv_rank, regime) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (batch_id, row.symbol, row.price, row.short_iv, row.base_iv, row.edge, row.hv_rank, row.regime))
                snap_id = c.lastrowid
                
                tag_val = row.tag 
                est_gamma = row.meta.get("est_gamma", 0.0)
                est_debit = bp.est_debit if bp else 0.0
                strat_name = bp.strategy if bp else "NONE"
                
                c.execute("""INSERT INTO trade_plans 
                          (snapshot_id, strategy_type, cal_score, short_risk, gate_status, est_debit, total_gamma, tag) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (snap_id, strat_name, row.cal_score, row.short_risk, gate, est_debit, est_gamma, tag_val))
                
            conn.commit()
            print(f"💾 [DB] Saved Batch {batch_id}: {count} items | AvgEdge: {avg_edge:.2f} | Time: {elapsed:.1f}s")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ [DB Error] Save failed: {e}")
        finally:
            conn.close()