import time
from datetime import datetime
from trade_guardian.app.cli import main

if __name__ == "__main__":
    # 1. 记录并打印开始时间
    start_time = time.time()
    start_dt = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n🔔 SYSTEM START: {start_dt}")
    
    # 2. 执行主程序
    main()
    
    # 3. 记录并打印结束时间及总耗时
    end_time = time.time()
    end_dt = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
    duration = end_time - start_time
    
    print("\n" + "=" * 105)
    print(f"🔕 SYSTEM END:   {end_dt}")
    print(f"⏱️  TOTAL ELAPSED: {duration:.2f} seconds")
    print("=" * 105 + "\n")