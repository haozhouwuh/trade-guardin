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
                    urgency: str = "PASSIVE"
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
        if strategy in ["STRADDLE", "LG", "LONG_GAMMA", "AUTO-LG"]:
            # [FIX] 强制 range_val="ALL" 以确保获取所有 Strike
            chain_data = self.client._fetch_calls_chain(symbol, short_exp, short_exp, range_val="ALL")
            call_map = chain_data.get("callExpDateMap", {})
            
            # Recenter 逻辑 (只看 ATM)
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
            
            if not q_call or not q_put: return {"status": "FAIL", "msg": "Missing Quotes (Straddle)"}
            
            # 合成报价 (Debit = Call + Put)
            bid = q_call['bid'] + q_put['bid']
            ask = q_call['ask'] + q_put['ask']
            legs_desc = f"+{short_exp} {final_strike} STRADDLE"

        # B) 对角线策略 (DIAGONAL / PMCC)
        elif strategy in ["DIAGONAL", "PMCC", "AUTO-DIAG"]:
            if not long_exp or not long_strike:
                return {"status": "FAIL", "msg": "Diagonal requires long_exp and long_strike"}
            
            # [FIX] 关键修复：range_val="ALL"
            # QQQ 等高价股的 Long Leg 通常是深度实值，如果不加 ALL 会被 API 过滤掉
            chain_short = self.client._fetch_calls_chain(symbol, short_exp, short_exp, range_val="ALL")
            chain_long = self.client._fetch_calls_chain(symbol, long_exp, long_exp, range_val="ALL")
            
            # Call Diagonal: Long Call (Far) - Short Call (Near)
            q_short = self._extract_quote(chain_short, "callExpDateMap", short_exp, short_strike)
            q_long = self._extract_quote(chain_long, "callExpDateMap", long_exp, long_strike)
            
            if not q_short or not q_long: 
                # 诊断信息：帮助确认是哪一条腿缺了
                missing = []
                if not q_short: missing.append(f"Short({short_exp} {short_strike})")
                if not q_long: missing.append(f"Long({long_exp} {long_strike})")
                return {"status": "FAIL", "msg": f"Missing Diagonal Quotes: {', '.join(missing)}"}
            
            # 合成报价 (Debit = Long Ask - Short Bid)
            # 实际上 Mid Calculation:
            mid_short = (q_short['bid'] + q_short['ask']) / 2.0
            mid_long = (q_long['bid'] + q_long['ask']) / 2.0
            
            # Debit = Long - Short
            mid_price = mid_long - mid_short
            
            # Spread 计算：悲观 Bid (卖出价) / 悲观 Ask (买入价)
            bid = q_long['bid'] - q_short['ask'] 
            ask = q_long['ask'] - q_short['bid'] 
            
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

        # 动态定价策略
        tick = self._get_tick_size(comp_mid)
        
        if urgency == "AGGRESSIVE":
            target_price = ask 
            desc = "AGGRESSIVE (Hit Ask)"
            color = Fore.RED
            
        elif urgency == "NEUTRAL":
            target_price = comp_mid
            desc = "NEUTRAL (Fair Value)"
            color = Fore.YELLOW
            
        else: # PASSIVE (默认)
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
        """
        Helper to dig out quote from raw chain dict.
        Contains Robust Matching logic for strikes (Float vs String mismatch).
        """
        exp_map = chain.get(map_key, {})
        
        # 1. 找到对应的 Expiry Date Key (例如 "2025-12-26:4")
        target_exp_key = None
        for k in exp_map.keys():
            if k.startswith(exp):
                target_exp_key = k
                break
        
        if not target_exp_key:
            return None
            
        strikes_map = exp_map[target_exp_key]
        target_strike = float(strike)
        
        # 2. 遍历所有 Strike 寻找匹配 (容错 0.01)
        # 解决 API 返回 "619.0" 而目标是 619 的 key 不匹配问题
        for s_str, q_list in strikes_map.items():
            try:
                s_val = float(s_str)
                if abs(s_val - target_strike) < 0.01:
                    return q_list[0]
            except ValueError:
                continue
                
        return None