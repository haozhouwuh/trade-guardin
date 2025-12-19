import sqlite3
import pandas as pd
import sys
import time
from tabulate import tabulate
from colorama import Fore, Style, init

# 初始化 colorama
init(autoreset=True)

class HistoryViewer:
    def __init__(self, db_path="db/trade_guardian.db"):
        self.db_path = db_path

    def get_latest_radar(self, symbol=None, limit=20):
        """
        核心逻辑：支持个股查询 (symbol) 并保持所有动能列
        """
        conn = sqlite3.connect(self.db_path)
        latest_res = conn.execute("SELECT MAX(batch_id) FROM scan_batches").fetchone()
        if not latest_res or latest_res[0] is None:
            conn.close()
            return pd.DataFrame()
        
        latest_batch = latest_res[0]
        
        # 兼容性查询逻辑
        where_clause = "WHERE b.batch_id = ?"
        params = [latest_batch]
        
        if symbol:
            where_clause += " AND s.symbol = ?"
            params.append(symbol.upper())
        
        query = f"""
        SELECT 
            strftime('%H:%M:%S', b.timestamp) as Time,
            s.symbol as Sym,
            s.price as Price,
            s.iv_short as IV_S,
            '--' as DNA,
            0.0 as Δ15m,
            0.0 as Δ1h,
            0.0 as VIX,
            0.0 as VIX_Δ,
            p.gate_status as Gate,
            p.total_gamma as Gamma,
            p.cal_score as Score
        FROM market_snapshots s
        JOIN scan_batches b ON s.batch_id = b.batch_id
        JOIN trade_plans p ON s.snapshot_id = p.snapshot_id
        {where_clause}
        ORDER BY p.cal_score DESC, s.symbol ASC
        LIMIT ?
        """
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        
        # 计算动能与注入 DNA
        if not df.empty:
            df = self._calculate_dynamics_and_dna(df, latest_batch, conn)
        
        conn.close()
        return df

    def _calculate_dynamics_and_dna(self, df, latest_id, conn):
        """
        精准恢复 Δ15m/Δ1h 动能与 DNA 标签
        """
        for i, row in df.iterrows():
            # 获取 15m (1 batch ago) 和 1h (4 batches ago) 数据
            res_15m = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (row['Sym'], latest_id - 1)).fetchone()
            res_1h = conn.execute("SELECT iv_short FROM market_snapshots WHERE symbol=? AND batch_id=?", (row['Sym'], latest_id - 4)).fetchone()

            d15 = 0.0
            if res_15m:
                d15 = round(row['IV_S'] - res_15m[0], 1)
                df.at[i, 'Δ15m'] = d15
            
            if res_1h:
                df.at[i, 'Δ1h'] = round(row['IV_S'] - res_1h[0], 1)

            # DNA 判定逻辑
            if d15 > 2.0: df.at[i, 'DNA'] = f"{Fore.CYAN}PULSE🔥"
            elif d15 > 0.5: df.at[i, 'DNA'] = f"{Fore.GREEN}TREND🚀"
            elif d15 < -1.0: df.at[i, 'DNA'] = f"{Fore.YELLOW}CRUSH❄️"
            else: df.at[i, 'DNA'] = f"{Fore.WHITE}QUIET⏳"
                
        return df

    def display(self, symbol=None):
        df = self.get_latest_radar(symbol=symbol)
        if df.empty:
            print(f"📭 No data found for {symbol if symbol else 'latest batch'}.")
            return

        # 视觉高亮
        df['Δ15m_fmt'] = df['Δ15m'].apply(lambda x: f"{Fore.RED}+{x}🔥" if x > 1.5 else (f"{Fore.CYAN}{x}❄️" if x < -1.5 else f"{x}"))
        df['Gate'] = df['Gate'].apply(lambda x: f"{Fore.GREEN}{x}" if x == 'EXEC' else f"{Fore.YELLOW}{x}")
        df['Score'] = df['Score'].apply(lambda x: f"{Fore.CYAN}{Style.BRIGHT}{x}" if x >= 70 else x)

        df_display = df.copy()
        df_display['Δ15m'] = df_display['Δ15m_fmt']
        
        print("\n" + "="*110)
        print(f"📡 DNA ENHANCED RADAR | {df['Time'].iloc[0]}")
        print("="*110)
        
        cols = ['Time', 'Sym', 'DNA', 'Price', 'IV_S', 'Δ15m', 'Δ1h', 'Gamma', 'Score', 'Gate']
        print(tabulate(df_display[cols], headers='keys', tablefmt='psql', showindex=False))
        print("="*110)

if __name__ == "__main__":
    viewer = HistoryViewer()
    
    # 获取命令行参数
    target_sym = sys.argv[1] if len(sys.argv) > 1 else None
    
    # 修改逻辑：无论是否有参数，都进入循环监控模式
    while True:
        # 清屏（可选，让界面更整洁，Windows用cls，Linux/Mac用clear）
        # import os; os.system('cls' if os.name == 'nt' else 'clear')
        
        viewer.display(symbol=target_sym)
        
        mode_desc = f"STOCKS: {target_sym.upper()}" if target_sym else "ALL RADAR"
        print(f"\n📡 MODE: {mode_desc}")
        print(f"⏳ Sleeping 15 min. Next refresh at: {time.strftime('%H:%M:%S', time.localtime(time.time() + 900))}")
        
        try:
            time.sleep(900)  # 15分钟循环
        except KeyboardInterrupt:
            print("\n👋 Monitoring stopped by user.")
            break