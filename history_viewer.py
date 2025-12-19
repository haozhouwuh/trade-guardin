import sqlite3
import pandas as pd
import sys
import time
from tabulate import tabulate
from colorama import Fore, Style, init

# 初始化颜色环境
init(autoreset=True)

class HistoryViewer:
    def __init__(self, db_path="db/trade_guardian.db"):
        self.db_path = db_path

    def get_latest_radar(self, symbol=None, limit=20):
        """
        锁定最新物理批次
        """
        conn = sqlite3.connect(self.db_path)
        batch_res = conn.execute("""
            SELECT b.batch_id, strftime('%H:%M:%S', b.timestamp), b.market_vix 
            FROM scan_batches b
            JOIN market_snapshots s ON s.batch_id = b.batch_id
            ORDER BY b.batch_id DESC LIMIT 1
        """).fetchone()
        
        if not batch_res:
            conn.close()
            return pd.DataFrame()
        
        latest_id, latest_time, current_vix = batch_res
        
        query = f"""
        SELECT 
            s.symbol as Sym,
            s.price as Price,
            s.iv_short as IV_S,
            s.snapshot_id,
            COALESCE(p.gate_status, 'WAIT') as Gate,
            COALESCE(p.cal_score, 0) as Score,
            COALESCE(p.total_gamma, 0.0) as Gamma
        FROM market_snapshots s
        LEFT JOIN trade_plans p ON s.snapshot_id = p.snapshot_id
        WHERE s.batch_id = ?
        ORDER BY Score DESC, IV_S DESC
        LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(latest_id, limit))
        
        if not df.empty:
            df['Time'] = latest_time
            df['VIX'] = current_vix
            df = self._process_logic(df, latest_id, conn)
        
        conn.close()
        return df

    def _process_logic(self, df, latest_id, conn):
        """
        动能计算与裁决同步
        """
        v_prev = conn.execute("SELECT market_vix FROM scan_batches WHERE batch_id = ?", (latest_id-1,)).fetchone()
        df['VIX_Δ'] = round(df['VIX'].iloc[0] - v_prev[0], 2) if v_prev else 0.0

        for i, row in df.iterrows():
            sym = row['Sym']
            res15 = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (sym, latest_id-1)).fetchone()
            res1h = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (sym, latest_id-4)).fetchone()

            d15 = round(row['IV_S'] - (res15[0] if res15 else row['IV_S']), 1)
            d1h = round(row['IV_S'] - (res1h[0] if res1h else row['IV_S']), 1)
            
            df.at[i, 'Δ15m'] = d15
            df.at[i, 'Δ1h'] = d1h

            # DNA 判定 (文字对齐)
            dna_type = "QUIET"
            if d15 > 2.0: dna_type = "PULSE"
            elif d15 > 0.5: dna_type = "TREND"
            elif d15 < -1.0: dna_type = "CRUSH"
            
            df.at[i, 'DNA_Raw'] = dna_type

            if row['Gate'] == "EXEC" and dna_type == "QUIET":
                df.at[i, 'Gate'] = "WAIT"
                
        return df

    def display(self, symbol=None):
        df = self.get_latest_radar(symbol=symbol)
        if df.empty:
            print(f"{Fore.RED}📭 [Sync] Monitoring...{Style.RESET_ALL}")
            return

        formatted_rows = []
        for _, row in df.iterrows():
            # 1. 物理锁死：构造没有任何颜色代码的纯等宽字符串
            # 强制要求：所有数值必须有固定的总宽度（含空格和小数点）
            p_str = f"{row['Price']:>10.2f}"
            iv_str = f"{row['IV_S']:>8.1f}%"
            d15_raw = f"{row['Δ15m']:>+6.1f}"
            d1h_raw = f"{row['Δ1h']:>+6.1f}"
            g_str = f"{row['Gamma']:>8.3f}"  # 强制 3 位小数
            s_str = f"{row['Score']:>5}"
            
            # 2. 隔离染色：将染色代码包裹在等宽字符串之外
            d15_render = d15_raw
            if row['Δ15m'] > 1.5: 
                d15_render = f"{Fore.RED}{d15_raw}{Style.RESET_ALL}"
            elif row['Δ15m'] < -1.5: 
                d15_render = f"{Fore.CYAN}{d15_raw}{Style.RESET_ALL}"

            # DNA 状态染色
            dna_raw = f"{row['DNA_Raw']:<6}" # 强制 6 位宽
            if row['DNA_Raw'] == "PULSE": dna_render = f"{Fore.CYAN}{dna_raw}{Style.RESET_ALL}"
            elif row['DNA_Raw'] == "TREND": dna_render = f"{Fore.GREEN}{dna_raw}{Style.RESET_ALL}"
            elif row['DNA_Raw'] == "CRUSH": dna_render = f"{Fore.YELLOW}{dna_raw}{Style.RESET_ALL}"
            else: dna_render = f"{Fore.WHITE}{dna_raw}{Style.RESET_ALL}"

            # Gate 染色
            gate_raw = f"{row['Gate']:<6}"
            gate_c = Fore.GREEN if row['Gate'] == "EXEC" else (Fore.YELLOW if row['Gate'] == "WAIT" else Fore.RED)
            gate_render = f"{gate_c}{gate_raw}{Style.RESET_ALL}"

            formatted_rows.append([
                f"{Fore.LIGHTBLACK_EX}{row['Time']}{Style.RESET_ALL}",
                f"{Style.BRIGHT}{row['Sym']:<6}{Style.RESET_ALL}",
                dna_render,
                p_str,
                iv_str,
                d15_render,
                d1h_raw,
                g_str,
                f"{Fore.CYAN if row['Score'] >= 70 else Fore.WHITE}{s_str}{Style.RESET_ALL}",
                gate_render
            ])

        # 3. 头部信息
        v_diff = df['VIX_Δ'].iloc[0]
        v_info = f" | VIX: {df['VIX'].iloc[0]} ({Fore.RED if v_diff > 0 else Fore.GREEN}{v_diff:+0.2f}{Style.RESET_ALL})"
        
        print("\n" + "="*112)
        print(f"📡 DNA MOMENTUM RADAR | {df['Time'].iloc[0]}{v_info}")
        print("="*112)
        
        # 4. 终极参数：stralign="left" 禁止 tabulate 重新计算对齐
        # disable_numparse=True 确保 0.030 不会被识别为 0.03
        headers = ["Time", "Sym", "DNA", "Price", "IV_S", "Δ15m", "Δ1h", "Gamma", "Score", "Gate"]
        print(tabulate(formatted_rows, headers=headers, tablefmt='psql', stralign="left", disable_numparse=True))
        print("\n" + "="*112)

        
if __name__ == "__main__":
    viewer = HistoryViewer()
    target_sym = sys.argv[1] if len(sys.argv) > 1 else None
    while True:
        viewer.display(symbol=target_sym)
        try:
            time.sleep(60)
        except KeyboardInterrupt: break