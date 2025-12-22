import sys
import os

# 1. 路径设置
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(current_dir, "src"))

from trade_guardian.infra.config import load_config, DEFAULT_CONFIG
from trade_guardian.infra.schwab_client import SchwabClient
from trade_guardian.action.sniper import Sniper

# 2. 初始化
print("🚀 Initializing Trade Guardian Action Test...")
cfg = load_config("config/config.json", DEFAULT_CONFIG)
client = SchwabClient(cfg)
sniper = Sniper(client)

# ==========================================
# 3. 测试场景：NVDA Diagonal (模拟 Radar 信号)
# ==========================================
symbol = "NVDA"
strategy = "DIAGONAL"

# Radar 推荐参数
short_exp = "2025-12-26"
short_strike = 185.0  # 卖出近端 OTM Call

long_exp = "2026-01-23" # 远端 Month Anchor
long_strike = 180.0   # 买入远端 ITM Call

# Fire!
result = sniper.lock_target(
    symbol=symbol, 
    strategy=strategy, 
    short_exp=short_exp, 
    short_strike=short_strike,
    long_exp=long_exp,
    long_strike=long_strike
)

print("\n✅ Final Result:", result)