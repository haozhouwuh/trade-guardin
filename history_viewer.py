import os
import sqlite3
import pandas as pd
import sys
import time
from datetime import datetime
from tabulate import tabulate
from colorama import Fore, Style, init

# 初始化颜色环境
init(autoreset=True)

class HistoryViewer:
    def __init__(self, db_path=None):
        if db_path:
            self.db_path = db_path
        else:
            root = os.path.dirname(os.path.abspath(__file__))
            self.db_path = os.path.join(root, "db", "trade_guardian.db")

    def get_latest_radar(self, symbol=None, limit=20):
        conn = sqlite3.connect(self.db_path)
        try:
            # 1. 获取最新 Batch
            batch_res = conn.execute("""
                SELECT b.batch_id, b.timestamp, b.market_vix 
                FROM scan_batches b
                JOIN market_snapshots s ON s.batch_id = b.batch_id
                ORDER BY b.batch_id DESC LIMIT 1
            """).fetchone()
            
            if not batch_res:
                return pd.DataFrame(), 0, "N/A"
            
            latest_id, latest_time_str, current_vix = batch_res
            latest_time = datetime.strptime(latest_time_str, "%Y-%m-%d %H:%M:%S")
            
            # 2. 智能计算 Uptime (Session Awareness)
            current_id = latest_id
            session_start_time = latest_time
            
            past_batches = conn.execute("""
                SELECT batch_id, timestamp FROM scan_batches 
                WHERE batch_id >= ? ORDER BY batch_id DESC
            """, (latest_id - 12,)).fetchall()
            
            for i in range(len(past_batches) - 1):
                curr = datetime.strptime(past_batches[i][1], "%Y-%m-%d %H:%M:%S")
                prev = datetime.strptime(past_batches[i+1][1], "%Y-%m-%d %H:%M:%S")
                diff = (curr - prev).total_seconds()
                if diff > 1200: # 20分钟断档
                    session_start_time = curr
                    break
                session_start_time = prev 

            uptime_min = (latest_time - session_start_time).total_seconds() / 60.0
            if uptime_min < 1: uptime_min = 1.0
            
            # 3. 构建查询
            filter_sql = "AND s.symbol = ?" if symbol else ""
            params = [latest_id, symbol, limit] if symbol else [latest_id, limit]
            
            query = f"""
            SELECT 
                s.symbol as Sym,
                s.price as Price,
                s.iv_short as IV_S,
                s.snapshot_id,
                COALESCE(p.gate_status, 'WAIT') as Gate,
                COALESCE(p.cal_score, 0) as Score,
                COALESCE(p.total_gamma, 0.0) as Gamma,
                COALESCE(p.tag, '') as Tag,
                p.error_msg as Reason
            FROM market_snapshots s
            LEFT JOIN trade_plans p ON s.snapshot_id = p.snapshot_id
            WHERE s.batch_id = ? {filter_sql}
            ORDER BY Score DESC, IV_S DESC
            LIMIT ?
            """
            
            df = pd.read_sql_query(query, conn, params=tuple(params))
            
            if not df.empty:
                df['Time'] = latest_time_str
                df['VIX'] = current_vix
                df = self._process_logic(df, latest_id, conn, uptime_min)
            
            return df, uptime_min, latest_time_str
        finally:
            conn.close()


    def _process_logic(self, df, latest_id, conn, uptime_min):
        v_prev = conn.execute("SELECT market_vix FROM scan_batches WHERE batch_id = ?", (latest_id-1,)).fetchone()
        df['VIX_Δ'] = round(df['VIX'].iloc[0] - v_prev[0], 2) if v_prev else 0.0

        for i, row in df.iterrows():
            sym = row['Sym']
            
            res10 = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (sym, latest_id-1)).fetchone()
            res1h = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (sym, latest_id-6)).fetchone()

            # Δ10m Logic
            if res10:
                df.at[i, 'Δ10m'] = round(row['IV_S'] - res10[0], 1)
                df.at[i, 'Δ10m_Valid'] = True
            else:
                df.at[i, 'Δ10m'] = 0.0
                df.at[i, 'Δ10m_Valid'] = False 

            # Δ1h Logic
            if res1h:
                df.at[i, 'Δ1h'] = round(row['IV_S'] - res1h[0], 1)
                df.at[i, 'Δ1h_Valid'] = True
            else:
                df.at[i, 'Δ1h'] = 0.0
                df.at[i, 'Δ1h_Valid'] = False 

            # DNA 判定
            dna_type = "QUIET"
            d10 = df.at[i, 'Δ10m']
            
            if uptime_min < 60:
                # WARMUP 模式
                if d10 > 2.5: dna_type = "PULSE"
                elif d10 > 0.8: dna_type = "TREND"
                elif d10 < -1.5: dna_type = "CRUSH"
            else:
                # NORMAL 模式
                if d10 > 2.0: dna_type = "PULSE"
                elif d10 > 0.5: dna_type = "TREND"
                elif d10 < -1.0: dna_type = "CRUSH"
            
            df.at[i, 'DNA_Raw'] = dna_type
                
        return df

    def display(self, symbol=None):
        df, uptime_min, last_time = self.get_latest_radar(symbol=symbol)
        
        if df.empty:
            print(f"{Fore.RED}📭 [Sync] Monitoring... (No Data Yet){Style.RESET_ALL}")
            return

        formatted_rows = []
        for _, row in df.iterrows():
            p_str = f"{row['Price']:>8.1f}"
            iv_str = f"{row['IV_S']:>5.1f}%"
            g_str = f"{row['Gamma']:>6.3f}"
            s_str = f"{row['Score']:>3}"
            
            # Δ10m
            if row['Δ10m_Valid']:
                d10_val = row['Δ10m']
                d10_str = f"{d10_val:>+5.1f}"
                if d10_val > 1.5: d10_render = f"{Fore.RED}{d10_str}{Style.RESET_ALL}"
                elif d10_val < -1.5: d10_render = f"{Fore.CYAN}{d10_str}{Style.RESET_ALL}"
                else: d10_render = d10_str
            else:
                d10_render = f"{Fore.LIGHTBLACK_EX} INIT{Style.RESET_ALL}"

            # Δ1h 强制 Warmup 检查
            if row['Δ1h_Valid'] and uptime_min >= 60:
                d1h_val = row['Δ1h']
                d1h_str = f"{d1h_val:>+5.1f}"
                d1h_render = d1h_str 
            else:
                d1h_render = f"{Fore.YELLOW} WARM{Style.RESET_ALL}"

            dna_raw = f"{row['DNA_Raw']:<5}"
            if row['DNA_Raw'] == "PULSE": dna_render = f"{Fore.CYAN}{dna_raw}{Style.RESET_ALL}"
            elif row['DNA_Raw'] == "TREND": dna_render = f"{Fore.GREEN}{dna_raw}{Style.RESET_ALL}"
            elif row['DNA_Raw'] == "CRUSH": dna_render = f"{Fore.YELLOW}{dna_raw}{Style.RESET_ALL}"
            else: dna_render = f"{Fore.WHITE}{dna_raw}{Style.RESET_ALL}"

            gate_raw = f"{row['Gate']:<6}"
            if row['Gate'] == "EXEC": gate_c = Fore.GREEN
            elif row['Gate'] == "LIMIT": gate_c = Fore.CYAN
            elif row['Gate'] == "FORBID": gate_c = Fore.RED
            else: gate_c = Fore.YELLOW
            gate_render = f"{gate_c}{gate_raw}{Style.RESET_ALL}"

            tag_val = row['Tag'] if row['Tag'] else ""
            tag_render = f"{Fore.WHITE}{tag_val:<9}{Style.RESET_ALL}"

            formatted_rows.append([
                f"{Style.BRIGHT}{row['Sym']:<5}{Style.RESET_ALL}",
                dna_render,
                p_str,
                iv_str,
                d10_render,
                d1h_render, 
                g_str,
                f"{Fore.CYAN if row['Score'] >= 70 else Fore.WHITE}{s_str}{Style.RESET_ALL}",
                gate_render,
                tag_render
            ])

        v_diff = df['VIX_Δ'].iloc[0]
        v_info = f"VIX: {df['VIX'].iloc[0]} ({Fore.RED if v_diff > 0 else Fore.GREEN}{v_diff:+0.2f}{Style.RESET_ALL})"
        
        mode_str = f"{Fore.GREEN}NORMAL{Style.RESET_ALL}"
        if uptime_min < 60:
            mode_str = f"{Fore.YELLOW}WARMUP (<60m){Style.RESET_ALL}"
        
        print("\n" + "="*100)
        print(f"📡 RADAR | {last_time} | Run: {int(uptime_min)}m | Mode: {mode_str} | {v_info}")
        print("="*100)
        
        headers = ["Sym", "DNA", "Price", "IV_S", "Δ10m", "Δ1h", "Gamma", "Scr", "Gate", "Tag"]
        print(tabulate(formatted_rows, headers=headers, tablefmt='simple', stralign="right", disable_numparse=True))
        print("-" * 100)

        # --- [FIX] 修复诊断区逻辑：如果 Reason 为空，给予默认值，而不是隐藏 ---
        problematic = df[ (df['Gate'] == 'FORBID') | (df['Reason'].notna()) ]
        if not problematic.empty:
            print(f"{Fore.RED}⛔ Risk / Gate Diagnostics:{Style.RESET_ALL}")
            for _, r in problematic.iterrows():
                # 如果数据库里 error_msg 是空的，提供一个默认的兜底解释
                # IWM 这种 Gamma 超标的通常属于 Policy Restriction
                reason_str = r['Reason'] if r['Reason'] else "Policy Restriction (High Risk/Gamma)"
                print(f"   • {Style.BRIGHT}{r['Sym']}{Style.RESET_ALL}: {r['Gate']} -> {reason_str}")
        
        print("="*100)

        
if __name__ == "__main__":
    viewer = HistoryViewer()
    target_sym = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Starting Dashboard... (Target: {target_sym if target_sym else 'ALL'})")
    while True:
        try:
            viewer.display(symbol=target_sym)
            time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)