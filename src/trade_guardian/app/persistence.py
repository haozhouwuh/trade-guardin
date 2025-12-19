import sqlite3
import os
import json
from datetime import datetime
from typing import List, Tuple, Optional, Any
from trade_guardian.domain.models import ScanRow, Blueprint

class PersistenceManager:
    def __init__(self, db_path: str = None):
        if db_path is None:
            # 自动定位到项目根目录下的 db/trade_guardian.db
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            db_path = os.path.join(root, "db", "trade_guardian.db")
        self.db_path = db_path

    def save_scan_session(self, 
                          strategy_name: str, 
                          vix: float, 
                          universe_size: int, 
                          avg_edge: float, 
                          cheap_pct: float,
                          elapsed: float,
                          results: List[Tuple[ScanRow, Any, Optional[Blueprint], str]]) -> int:
        """
        全量保存扫描会话并返回当前 batch_id
        """
        if not os.path.exists(self.db_path):
            print(f"⚠️ [Database] File not found: {self.db_path}")
            return 0

        conn = sqlite3.connect(self.db_path)
        batch_id = 0
        try:
            c = conn.cursor()
            timestamp = datetime.now().isoformat()

            # 1. 插入批次概览
            c.execute('''
                INSERT INTO scan_batches 
                (timestamp, strategy_name, market_vix, universe_size, avg_abs_edge, cheap_vol_pct, elapsed_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (timestamp, strategy_name, vix, universe_size, avg_edge, cheap_pct, elapsed))
            
            batch_id = c.lastrowid

            # 2. 遍历结果保存快照与计划
            for row, ctx, bp, gate in results:
                # 记录市场快照
                c.execute('''
                    INSERT INTO market_snapshots 
                    (batch_id, symbol, price, iv_short, iv_base, hv_rank, regime)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (batch_id, row.symbol, row.price, row.short_iv, row.base_iv, row.hv_rank, row.regime))
                
                snapshot_id = c.lastrowid

                # 序列化蓝图
                bp_json = "{}"
                if bp:
                    bp_data = {
                        "strategy": bp.strategy,
                        "est_debit": bp.est_debit,
                        "legs": [{"action": l.action, "exp": l.exp, "strike": l.strike, "type": l.type} for l in bp.legs]
                    }
                    bp_json = json.dumps(bp_data)

                # 判定子策略类型与 Gamma 提取
                stype = "LG" if "LG" in row.tag else "PMCC"
                total_gamma = row.meta.get("est_gamma", 0.0)

                # 记录详细交易计划
                c.execute('''
                    INSERT INTO trade_plans 
                    (snapshot_id, strategy_type, cal_score, short_risk, gate_status, total_gamma, est_debit, error_msg, blueprint_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (snapshot_id, stype, row.cal_score, row.short_risk, gate.strip(), total_gamma, 
                      bp.est_debit if bp else 0.0, bp.error if bp else None, bp_json))

            conn.commit()
            print(f"💾 [DB] Persistent Success. Batch ID: {batch_id}")
        except Exception as e:
            print(f"❌ [DB] Save Error: {e}")
            conn.rollback()
        finally:
            conn.close()
        return batch_id

    def check_iv_spikes(self, current_batch_id: int, threshold: float = 3.0) -> List[Tuple[str, float]]:
        """
        [Query Tool] 对比当前批次与约1小时前 (4个batch) 的 IV 差异
        用于触发 VOLATILITY ALERT
        """
        if current_batch_id <= 1: return []
        
        conn = sqlite3.connect(self.db_path)
        lookback_id = max(1, current_batch_id - 4)
        
        query = f"""
        SELECT c.symbol, (c.iv_short - p.iv_short) as drift
        FROM market_snapshots c
        JOIN market_snapshots p ON c.symbol = p.symbol
        WHERE c.batch_id = {current_batch_id} AND p.batch_id = {lookback_id}
          AND (c.iv_short - p.iv_short) >= {threshold}
        """
        try:
            res = conn.execute(query).fetchall()
            return res
        except:
            return []
        finally:
            conn.close()

    def get_latest_drift_1h(self, symbol: str) -> float:
        """
        [NEW] 获取指定标的 1 小时级别的 IV 漂移值，用于支持 Vol Slingshot 逻辑
        """
        conn = sqlite3.connect(self.db_path)
        try:
            c = conn.cursor()
            # 获取最新的 batch_id
            c.execute("SELECT MAX(batch_id) FROM scan_batches")
            curr_id = c.fetchone()[0]
            if not curr_id or curr_id <= 1: return 0.0
            
            # 对比 1 小时前 (4 个批次)
            lookback_id = max(1, curr_id - 4)
            
            query = """
                SELECT (c.iv_short - p.iv_short) 
                FROM market_snapshots c
                JOIN market_snapshots p ON c.symbol = p.symbol
                WHERE c.symbol = ? AND c.batch_id = ? AND p.batch_id = ?
            """
            res = c.execute(query, (symbol.upper(), curr_id, lookback_id)).fetchone()
            return res[0] if res else 0.0
        except:
            return 0.0
        finally:
            conn.close()