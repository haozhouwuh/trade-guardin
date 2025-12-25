import sqlite3
import os
import json
from datetime import datetime
from dataclasses import asdict

class PersistenceManager:
    def __init__(self, db_path=None):
        if db_path:
            self.db_path = db_path
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(base_dir, "..", "..", ".."))
            self.db_path = os.path.join(project_root, "db", "trade_guardian.db")

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def save_scan_session(self, strategy_name, vix, count, avg_edge, cheap_vol, elapsed, results_pack):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            c.execute("""INSERT INTO scan_batches 
                      (timestamp, strategy_name, market_vix, universe_size, avg_abs_edge, cheap_vol_pct, elapsed_time) 
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (current_time, strategy_name, vix, count, avg_edge, cheap_vol, elapsed))
            batch_id = c.lastrowid
            
            for item in results_pack:
                row, ctx, bp, gate = item
                
                c.execute("""INSERT INTO market_snapshots 
                          (batch_id, symbol, price, iv_short, iv_base, edge, hv_rank, regime) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (batch_id, row.symbol, row.price, row.short_iv, row.base_iv, row.edge, row.hv_rank, row.regime))
                snap_id = c.lastrowid
                
                tag_val = row.tag 
                est_gamma = row.meta.get("est_gamma", 0.0)
                est_debit = bp.est_debit if bp else 0.0
                strat_name = bp.strategy if bp else "NONE"
                
                bp_json_str = ""
                if bp:
                    try:
                        bp_dict = asdict(bp)
                        bp_json_str = json.dumps(bp_dict)
                    except Exception as e:
                        print(f"⚠️ Failed to serialize blueprint for {row.symbol}: {e}")

                c.execute("""INSERT INTO trade_plans 
                          (snapshot_id, strategy_type, cal_score, short_risk, gate_status, est_debit, total_gamma, tag, blueprint_json) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (snap_id, strat_name, row.cal_score, row.short_risk, gate, est_debit, est_gamma, tag_val, bp_json_str))
                
            conn.commit()
            print(f"💾 [DB] Saved Batch {batch_id}: {count} items | AvgEdge: {avg_edge:.2f} | Time: {elapsed:.1f}s")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ [DB Error] Save failed: {e}")
        finally:
            conn.close()

    def record_order(self, snapshot_id: int, symbol: str, strategy: str, 
                     limit_price: float, quantity: int, 
                     blueprint_json: str, tags: str, 
                     underlying_price: float, iv: float):
        """
        [V2 Refactor] 写入主交易表 + 拆解写入腿部表
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            # 1. 写入主表 active_trades
            c.execute("""
                INSERT INTO active_trades 
                (snapshot_id, symbol, strategy, status, created_at, initial_cost, quantity, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (snapshot_id, symbol, strategy, "WORKING", current_time, limit_price, quantity, tags))
            
            trade_id = c.lastrowid
            
            # 2. 解析 Blueprint 并写入 trade_legs
            try:
                bp_data = json.loads(blueprint_json)
                legs = bp_data.get("legs", [])
                
                for idx, leg in enumerate(legs):
                    c.execute("""
                        INSERT INTO trade_legs
                        (trade_id, leg_index, action, ratio, exp_date, strike, op_type, status, entry_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        trade_id,
                        idx,
                        leg.get('action'),
                        leg.get('ratio'),
                        leg.get('exp'),
                        float(leg.get('strike')),
                        leg.get('type'),
                        "OPEN",
                        0.0
                    ))
                    
            except Exception as e:
                print(f"⚠️ Failed to parse legs for DB: {e}")
            
            conn.commit()
            print(f"📝 [DB] Order Recorded: ID {trade_id} with {len(legs)} legs")
            return trade_id
            
        except Exception as e:
            print(f"❌ [DB Error] Failed to record order: {e}")
            return None
        finally:
            conn.close()

    def fetch_active_trades(self):
        """
        [V2 Refactor] 获取交易主表，并附带查询子表数据
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            # 1. 获取主表
            c.execute("""
                SELECT * FROM active_trades 
                WHERE status IN ('WORKING', 'OPEN')
                ORDER BY trade_id DESC
            """)
            trades = [dict(row) for row in c.fetchall()]
            
            # 2. 为每个交易填充 Legs
            for t in trades:
                c.execute("""
                    SELECT * FROM trade_legs 
                    WHERE trade_id = ? 
                    ORDER BY leg_index ASC
                """, (t['trade_id'],))
                legs = [dict(r) for r in c.fetchall()]
                t['legs'] = legs # 直接挂载 List[Dict]
                
            return trades
        except Exception as e:
            print(f"❌ [DB Error] Fetch trades failed: {e}")
            return []
        finally:
            conn.close()

    def update_trade_status(self, trade_id: int, new_status: str, fill_price: float = None):
        """
        [V2] 更新主状态，同时处理子状态
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            if new_status == "OPEN":
                # 确认成交
                # 这里我们假设 fill_price 就是 initial_cost 的最终值
                c.execute("""
                    UPDATE active_trades 
                    SET status = ?, updated_at = ?, initial_cost = ?
                    WHERE trade_id = ?
                """, (new_status, current_time, fill_price, trade_id))
                
                c.execute("UPDATE trade_legs SET status='OPEN' WHERE trade_id=?", (trade_id,))
                
            elif new_status == "CLOSED":
                # 平仓
                c.execute("""
                    UPDATE active_trades 
                    SET status = ?, updated_at = ?
                    WHERE trade_id = ?
                """, (new_status, current_time, trade_id))
                c.execute("UPDATE trade_legs SET status='CLOSED' WHERE trade_id=?", (trade_id,))
            
            conn.commit()
            return True
        except Exception as e:
            print(f"❌ [DB Error] Update failed: {e}")
            return False
        finally:
            conn.close()
            
    def update_leg_prices(self, trade_id: int, leg_updates: list):
        """
        leg_updates: [(leg_id, current_price), ...]
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        try:
            for leg_id, px in leg_updates:
                c.execute("UPDATE trade_legs SET current_price = ? WHERE leg_id = ?", (px, leg_id))
            conn.commit()
        finally:
            conn.close()


    # [NEW] 批量更新腿部的开仓价格 (用于 Confirm Fill 时记录单腿成本)
    def update_leg_entry_prices(self, trade_id: int, legs_data: list):
        """
        legs_data: list of dicts, must contain 'leg_index' and 'live_price'
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        try:
            for leg in legs_data:
                # 注意：这里我们把 live_price (当前市价) 作为 entry_price (入场价) 保存
                price = float(leg.get('live_price', 0.0) or 0.0)
                idx = leg.get('leg_index')
                
                if idx is not None:
                    c.execute("""
                        UPDATE trade_legs 
                        SET entry_price = ? 
                        WHERE trade_id = ? AND leg_index = ?
                    """, (price, trade_id, idx))
            
            conn.commit()
            print(f"💾 [DB] Updated Leg Entry Prices for Trade {trade_id}")
        except Exception as e:
            print(f"❌ [DB Error] Failed to update leg prices: {e}")
        finally:
            conn.close()