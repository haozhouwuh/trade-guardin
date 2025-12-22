import time
import sys
import os
from datetime import datetime

# 确保 src 目录在路径中，以便正确导入
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from trade_guardian.app.cli import main

def run_guardian_loop():
    """
    守护者模式：每 15 分钟自动执行一次全量扫描并存库
    """
    INTERVAL = 10 * 60  # 10 分钟 (600秒)
    
    print("="*80)
    print(f"🛡️  TRADE GUARDIAN - DAEMON MODE ACTIVE")
    print(f"⏰ Polling Interval: {INTERVAL/60} minutes")
    print(f"📂 Project Root: {project_root}")
    print("="*80)
    
    try:
        while True:
            start_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 模拟命令行参数给 cli.main()
            # 这里你可以根据需要调整默认参数
            sys.argv = [
                "trade_guardian.py", 
                "scanlist", 
                "--strategy", "auto", 
                "--days", "600", 
                "--detail",
                "--top", "10" # 自动模式下只显示最有价值的10个计划
            ]
            
            print(f"\n🔄 [LOOP START] {now_str}")
            
            try:
                # 执行原有的 cli 主函数
                main()
            except Exception as e:
                print(f"❌ Session Execution Error: {e}")

            elapsed = time.time() - start_ts
            wait_time = max(0, INTERVAL - elapsed)
            
            next_run = datetime.fromtimestamp(time.time() + wait_time).strftime('%H:%M:%S')
            
            print(f"\n✅ SESSION COMPLETE. Duration: {elapsed:.2f}s")
            print(f"⏳ Sleeping {wait_time/60:.1f} min. Next run at: {next_run} (Ctrl+C to stop)")
            
            time.sleep(wait_time)
            
    except KeyboardInterrupt:
        print("\n🛑 Guardian daemon stopped by user. exiting...")
        sys.exit(0)

if __name__ == "__main__":
    # 逻辑判定：
    # 1. 如果你输入 python src/trade_guardian.py scanlist ... (带参数) -> 运行一次就结束
    # 2. 如果你直接输入 python src/trade_guardian.py (不带参数) -> 进入15分钟轮询模式
    if len(sys.argv) > 1:
        main()
    else:
        run_guardian_loop()