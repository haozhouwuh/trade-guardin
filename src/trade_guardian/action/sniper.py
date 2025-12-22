import math
from typing import Dict, Any, Optional
from colorama import Fore, Style

from trade_guardian.infra.schwab_client import SchwabClient
from trade_guardian.action import sights, safety

class Sniper:
    def __init__(self, client: SchwabClient):
        self.client = client

    def _get_tick_size(self, price: float) -> float:
        if price < 3.0: return 0.01
        return 0.05

    def _round_to_tick(self, price: float, tick: float) -> float:
        return round(round(price / tick) * tick, 2)

    def lock_target(self, 
                    symbol: str, 
                    strategy: str, 
                    short_exp: str, 
                    short_strike: float,
                    long_exp: Optional[str] = None, 
                    long_strike: Optional[float] = None,
                    urgency: str = "PASSIVE"  # <--- [NEW] 新增参数: PASSIVE, NEUTRAL, AGGRESSIVE
                    ) -> Dict[str, Any]:
        """
        全能锁定逻辑：支持 Straddle 和 Diagonal
        """
        print(f"\n🔭 {Fore.CYAN}SNIPER: Locking Target for {symbol} ({strategy})...{Style.RESET_ALL}")
        
        # 1. 获取标的现价 & 校准
        quote_underlying = self.client.get_quote(symbol)
        current_price = float(quote_underlying.get('lastPrice', 0.0))
        if current_price == 0: return {"status": "FAIL", "msg": "No Spot Price"}
        print(f"   • Spot Price: {Fore.YELLOW}{current_price:.2f}{Style.RESET_ALL}")

        # --- 策略分支 ---
        
        # A) 跨式策略 (STRADDLE / LG)
        if strategy in ["STRADDLE", "LG", "LONG_GAMMA"]:
            # Recenter 逻辑... (简化版，只看 ATM)
            # Fetch chain for valid strikes
            chain_data = self.client._fetch_calls_chain(symbol, short_exp, short_exp)
            call_map = chain_data.get("callExpDateMap", {})
            valid_strikes = []
            for k, v in call_map.items():
                if k.startswith(short_exp):
                    valid_strikes = sorted([float(s) for s in v.keys()])
                    break
            
            final_strike, changed = sights.recenter_target(current_price, short_strike, valid_strikes)
            if changed: print(f"   • Recenter: {short_strike} -> {final_strike}")
            
            # 报价
            q_call = self._extract_quote(chain_data, "callExpDateMap", short_exp, final_strike)
            q_put = self._extract_quote(chain_data, "putExpDateMap", short_exp, final_strike)
            
            if not q_call or not q_put: return {"status": "FAIL", "msg": "Missing Quotes"}
            
            # 合成报价 (Debit = Call + Put)
            bid = q_call['bid'] + q_put['bid']
            ask = q_call['ask'] + q_put['ask']
            legs_desc = f"+{short_exp} {final_strike} STRADDLE"

        # B) 对角线策略 (DIAGONAL / PMCC)
        elif strategy in ["DIAGONAL", "PMCC", "AUTO-DIAG"]:
            if not long_exp or not long_strike:
                return {"status": "FAIL", "msg": "Diagonal requires long_exp and long_strike"}
            
            # Diagonal 不需要 Recenter Strike，因为它是基于特定 Delta 选的，而不是纯 ATM
            # 我们直接信任传入的参数，或者在此处加入复杂的 Delta 校验逻辑 (暂略)
            
            # 获取两头的报价
            # 为了省流量，这里假设我们需要 fetch 两次 chain 或者一次大的
            # 简单起见，我们 fetch 包含这两个日期的 chain
            # 注意：实际生产中 Schwab API fetch ALL range 比较大，这里为了演示逻辑简化处理
            chain_short = self.client._fetch_calls_chain(symbol, short_exp, short_exp)
            chain_long = self.client._fetch_calls_chain(symbol, long_exp, long_exp)
            
            # Call Diagonal: Long Call (Far) - Short Call (Near)
            q_short = self._extract_quote(chain_short, "callExpDateMap", short_exp, short_strike)
            q_long = self._extract_quote(chain_long, "callExpDateMap", long_exp, long_strike)
            
            if not q_short or not q_long: return {"status": "FAIL", "msg": "Missing Diagonal Quotes"}
            
            # 合成报价 (Debit = Long Ask - Short Bid) -> 这是最保守的买入价
            # 实际上 Mid Calculation:
            mid_short = (q_short['bid'] + q_short['ask']) / 2.0
            mid_long = (q_long['bid'] + q_long['ask']) / 2.0
            
            # Debit = Long - Short
            mid_price = mid_long - mid_short
            
            # Spread 计算有点复杂，简单估算：
            bid = q_long['bid'] - q_short['ask'] # 最悲观卖出价
            ask = q_long['ask'] - q_short['bid'] # 最悲观买入价
            
            legs_desc = f"+{long_exp} {long_strike}C / -{short_exp} {short_strike}C"
            
        else:
            return {"status": "FAIL", "msg": f"Unknown Strategy: {strategy}"}

        # --- 通用定价逻辑 ---
        comp_mid = (bid + ask) / 2.0
        comp_spread = ask - bid
        
        # 安全检查
        safe_res = safety.check_liquidity({'bid': bid, 'ask': ask})
        if not safe_res.passed:
            print(f"   • {Fore.RED}SAFETY BLOCK: {safe_res.reason}{Style.RESET_ALL}")
            return {"status": "FAIL", "msg": safe_res.reason}
            
        print(f"   • Liquidity: OK (Spread: {comp_spread:.2f})")

        # [MODIFIED] 动态定价策略
        tick = self._get_tick_size(comp_mid)
        
        if urgency == "AGGRESSIVE":
            # 激进模式：直接打 Ask 价 (或者 Mid + 溢价)，确保成交
            # 这里的 Ask 是我们要付出的最大代价 (因为是 Debit 策略)
            # 为了防止被做市商宰太狠，我们可以定在 Ask
            target_price = ask 
            desc = "AGGRESSIVE (Hit Ask)"
            color = Fore.RED
            
        elif urgency == "NEUTRAL":
            # 中性模式：不占便宜，也不吃亏，挂中间
            target_price = comp_mid
            desc = "NEUTRAL (Fair Value)"
            color = Fore.YELLOW
            
        else: # PASSIVE (默认)
            # 钓鱼模式：想省点钱
            improvement = max(tick, 0.03)
            target_price = comp_mid - improvement
            desc = "PASSIVE (Fishing)"
            color = Fore.CYAN

        limit_price = self._round_to_tick(target_price, tick)
        
        print(f"   • {Fore.GREEN}🎯 FIRE SOLUTION COMPUTED [{desc}]{Style.RESET_ALL}")
        print(f"     Legs: {legs_desc}")
        print(f"     Mkt: {bid:.2f}/{ask:.2f} (Mid {comp_mid:.2f}) -> Limit: {color}{limit_price:.2f}{Style.RESET_ALL}")

        return {
            "status": "READY",
            "symbol": symbol,
            "limit_price": limit_price,
            "est_cost": limit_price * 100
        }

    def _extract_quote(self, chain, map_key, exp, strike):
        """Helper to dig out quote from raw chain dict"""
        exp_map = chain.get(map_key, {})
        for k, v in exp_map.items():
            if k.startswith(exp):
                s_key = str(strike) # Schwab keys are weird sometimes
                # try exact string match
                if s_key in v: return v[s_key][0]
                # try float match
                for sk, qlist in v.items():
                    if abs(float(sk) - strike) < 0.01:
                        return qlist[0]
        return None