from __future__ import annotations

from typing import Dict, Any, Optional, List
from colorama import Fore, Style

from trade_guardian.infra.schwab_client import SchwabClient
from trade_guardian.action import sights, safety


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _pick_spot(quote_obj: Dict[str, Any]) -> float:
    # Schwab quote keys can vary
    for k in ("lastPrice", "last", "mark", "regularMarketLastPrice"):
        v = _safe_float(quote_obj.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _hard_mid(bid: float, ask: float, mark: float, last: float) -> float:
    # mark -> (bid+ask)/2 -> last -> 0
    if mark > 0:
        return mark
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if last > 0:
        return last
    return 0.0


def _norm_quote(q: Dict[str, Any]) -> Dict[str, float]:
    bid = _safe_float(q.get("bid"), 0.0)
    ask = _safe_float(q.get("ask"), 0.0)
    mark = _safe_float(q.get("mark"), 0.0)
    last = _safe_float(q.get("last"), 0.0)
    mid = _hard_mid(bid=bid, ask=ask, mark=mark, last=last)
    return {"bid": bid, "ask": ask, "mark": mark, "last": last, "mid": mid}


class Sniper:
    def __init__(self, client: SchwabClient):
        self.client = client

    def _get_tick_size(self, price: float) -> float:
        if price < 3.0:
            return 0.01
        return 0.05

    def _round_to_tick(self, price: float, tick: float) -> float:
        if tick <= 0:
            return round(price, 2)
        return round(round(price / tick) * tick, 2)

    def _fetch_chain_one_exp(self, symbol: str, exp: str) -> Dict[str, Any]:
        # 你当前 SchwabClient 的签名：_fetch_chain(symbol, from_d, to_d, range_val="ALL")
        return self.client._fetch_chain(symbol, exp, exp, range_val="ALL")

    def _list_strikes(self, exp_map: Dict[str, Any], exp: str) -> List[float]:
        target_exp_key = None
        for k in exp_map.keys():
            if str(k).startswith(exp):
                target_exp_key = k
                break
        if not target_exp_key:
            return []
        strikes_map = exp_map.get(target_exp_key) or {}
        out: List[float] = []
        for s in strikes_map.keys():
            try:
                out.append(float(s))
            except Exception:
                continue
        out.sort()
        return out

    def _extract_quote(self, chain: Dict[str, Any], map_key: str, exp: str, strike: float) -> Optional[Dict[str, Any]]:
        exp_map = chain.get(map_key, {}) or {}

        target_exp_key = None
        for k in exp_map.keys():
            if str(k).startswith(exp):
                target_exp_key = k
                break
        if not target_exp_key:
            return None

        strikes_map = exp_map.get(target_exp_key) or {}
        target = float(strike)

        for s_str, q_list in strikes_map.items():
            try:
                s_val = float(s_str)
            except Exception:
                continue
            if abs(s_val - target) < 0.01:
                if q_list and isinstance(q_list, list):
                    return q_list[0] or None
                return None

        return None

    def lock_target(
        self,
        symbol: str,
        strategy: str,
        short_exp: str,
        short_strike: float,
        long_exp: Optional[str] = None,
        long_strike: Optional[float] = None,
        urgency: str = "PASSIVE",
    ) -> Dict[str, Any]:
        
        strat = (strategy or "").strip().upper()
        urg = (urgency or "PASSIVE").strip().upper()

        # [FIX 1] 智能判断 Side (CALL/PUT)
        # 默认 CALL，如果策略名包含 PUT (BULL-PUT, BEAR-PUT, LONG-PUT), 则用 Put 链
        target_side = "CALL" 
        if "PUT" in strat:
            target_side = "PUT"
        
        map_key = "callExpDateMap" if target_side == "CALL" else "putExpDateMap"

        # [FIX 2] 智能判断 Credit/Debit
        # Vertical Credit Spreads: BULL-PUT, BEAR-CALL
        # Vertical Debit Spreads:  BULL-CALL, BEAR-PUT
        # Diagonals (PMCC): Usually Debit
        is_credit_spread = False
        if "BULL-PUT" in strat or "BEAR-CALL" in strat or "CREDIT" in strat or "IC" in strat or "IRON" in strat:
            is_credit_spread = True

        print(f"\n🔭 {Fore.CYAN}SNIPER: Locking {symbol} [{strat}] Mode:{urg} Side:{target_side} (Credit:{is_credit_spread}){Style.RESET_ALL}")

        quote_underlying = self.client.get_quote(symbol)
        current_price = _pick_spot(quote_underlying)
        if current_price <= 0:
            return {"status": "FAIL", "msg": "No Spot Price"}

        print(f"   • Spot Price: {Fore.YELLOW}{current_price:.2f}{Style.RESET_ALL}")

        bid: float = 0.0
        ask: float = 0.0
        comp_mid: float = 0.0
        legs_desc: str = ""
        final_short_strike: float = float(short_strike)

        # A) STRADDLE / LG (双买，Debit)
        if strat in {"STRADDLE", "LG", "LONG_GAMMA", "AUTO-LG", "AUTO_LG"}:
            chain_data = self._fetch_chain_one_exp(symbol=symbol, exp=short_exp)
            # Straddle 需要 Call 和 Put 两边
            call_map = chain_data.get("callExpDateMap", {}) or {}
            
            valid_strikes = self._list_strikes(call_map, short_exp)
            if not valid_strikes:
                return {"status": "FAIL", "msg": "No Strikes (Straddle)"}

            final_short_strike, changed = sights.recenter_target(
                current_price, float(short_strike), valid_strikes
            )
            if changed:
                print(f"   • Recenter: {short_strike} -> {final_short_strike}")

            q_call_raw = self._extract_quote(chain_data, "callExpDateMap", short_exp, final_short_strike)
            q_put_raw = self._extract_quote(chain_data, "putExpDateMap", short_exp, final_short_strike)
            if not q_call_raw or not q_put_raw:
                return {"status": "FAIL", "msg": "Missing Quotes (Straddle)"}

            q_call = _norm_quote(q_call_raw)
            q_put = _norm_quote(q_put_raw)

            if q_call["bid"] <= 0 or q_call["ask"] <= 0 or q_put["bid"] <= 0 or q_put["ask"] <= 0:
                return {"status": "FAIL", "msg": "Zero Liquidity (Straddle legs)"}

            bid = q_call["bid"] + q_put["bid"]
            ask = q_call["ask"] + q_put["ask"]
            comp_mid = q_call["mid"] + q_put["mid"]
            legs_desc = f"+{short_exp} {final_short_strike}C +{short_exp} {final_short_strike}P"

        # B) DIAGONAL / VERTICAL (Double Leg)
        elif "DIAGONAL" in strat or "PMCC" in strat or "BULL" in strat or "BEAR" in strat or "VERT" in strat or "IC" in strat:
            if not long_exp or long_strike is None:
                return {"status": "FAIL", "msg": "Double Leg requires long_exp and long_strike"}

            # Fetch Chains
            # 如果 exp 相同，只 fetch 一次
            if short_exp == long_exp:
                chain_short = self._fetch_chain_one_exp(symbol=symbol, exp=short_exp)
                chain_long = chain_short
            else:
                chain_short = self._fetch_chain_one_exp(symbol=symbol, exp=short_exp)
                chain_long = self._fetch_chain_one_exp(symbol=symbol, exp=long_exp)

            # [FIX 3] 使用动态 map_key (putExpDateMap 或 callExpDateMap)
            q_short_raw = self._extract_quote(chain_short, map_key, short_exp, float(short_strike))
            q_long_raw = self._extract_quote(chain_long, map_key, long_exp, float(long_strike))

            if not q_short_raw or not q_long_raw:
                missing = []
                if not q_short_raw: missing.append(f"Short({short_exp} {short_strike})")
                if not q_long_raw: missing.append(f"Long({long_exp} {long_strike})")
                return {"status": "FAIL", "msg": f"Missing Quotes: {', '.join(missing)}"}

            q_short = _norm_quote(q_short_raw)
            q_long = _norm_quote(q_long_raw)

            # Liquidity Check
            if q_short["bid"] <= 0 or q_short["ask"] <= 0 or q_long["bid"] <= 0 or q_long["ask"] <= 0:
                return {"status": "FAIL", "msg": "Zero Liquidity (Legs)"}

            # [FIX 4] 计算逻辑区分 Debit / Credit
            if is_credit_spread:
                # Credit Spread: Sell Short (Expensive), Buy Long (Cheap)
                # Formula: Short - Long
                # Price is POSITIVE (Credit Received)
                # Bid = Short_Bid - Long_Ask (保守卖价)
                bid = q_short["bid"] - q_long["ask"]
                # Ask = Short_Ask - Long_Bid (保守买价)
                ask = q_short["ask"] - q_long["bid"]
                comp_mid = q_short["mid"] - q_long["mid"]
                
                # legs_desc: 卖 Short / 买 Long
                legs_desc = f"-{short_exp} {float(short_strike)} {target_side} / +{long_exp} {float(long_strike)} {target_side}"
            else:
                # Debit Spread: Buy Long (Expensive), Sell Short (Cheap)
                # Formula: Long - Short
                # Price is POSITIVE (Debit Paid)
                bid = q_long["bid"] - q_short["ask"]
                ask = q_long["ask"] - q_short["bid"]
                comp_mid = q_long["mid"] - q_short["mid"]
                
                # legs_desc: 买 Long / 卖 Short
                legs_desc = f"+{long_exp} {float(long_strike)} {target_side} / -{short_exp} {float(short_strike)} {target_side}"

        else:
            return {"status": "FAIL", "msg": f"Unknown Strategy: {strategy}"}

        if comp_mid <= 0 and bid > 0 and ask > 0:
            comp_mid = (bid + ask) / 2.0

        comp_spread = ask - bid

        safe_res = safety.check_liquidity({"bid": bid, "ask": ask}, strict_mode=False)
        if not safe_res.passed:
            # 仅打印警告，不强制阻止，为了方便调试
            print(f"   • {Fore.YELLOW}SAFETY WARN: {safe_res.reason}{Style.RESET_ALL}")

        print(f"   • Liquidity: Spread {comp_spread:.2f} (Mid {comp_mid:.2f})")

        tick = self._get_tick_size(comp_mid)

        # [FIX 5] Pricing Mode 适配 Credit/Debit
        if urg == "AGGRESSIVE":
            # Credit: Aggressive = Hit Bid (Accept less credit to fill now) -> 其实是 Natural Price
            # Debit:  Aggressive = Hit Ask (Pay more to fill now)
            # 这里的 bid/ask 已经是计算过的组合价
            # Credit Spread 的 'ask' 是对手价，'bid' 是自然价？
            # 这里的 bid/ask 变量名代表的是 spread 的 bid/ask
            # 对于 Credit Spread (Sell): 我们是 Seller，Aggressive = Sell at Bid
            # 对于 Debit Spread (Buy): 我们是 Buyer，Aggressive = Buy at Ask
            # 之前的代码里 bid/ask 计算是：
            # Credit: bid = short_bid - long_ask. 这是我们能卖到的最低价 (Natural Bid)
            # Debit:  ask = long_ask - short_bid. 这是我们需付的最高价 (Natural Ask)
            
            if is_credit_spread:
                target_price = bid 
            else:
                target_price = ask
            desc = "AGGRESSIVE (Market/Nat)"
            color = Fore.RED
            
        elif urg == "NEUTRAL":
            target_price = comp_mid
            desc = "NEUTRAL (Mid)"
            color = Fore.YELLOW
            
        else: # PASSIVE
            # Debit: Mid - tick (想少付钱)
            # Credit: Mid + tick (想多收钱)
            improvement = max(tick, 0.03)
            if is_credit_spread:
                target_price = comp_mid + improvement
            else:
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
            "strategy": strat,
            "limit_price": float(limit_price),
            "est_cost": float(limit_price) * 100.0,
            "bid": float(bid),
            "ask": float(ask),
            "mid": float(comp_mid),
            "spread": float(comp_spread),
            "legs_desc": legs_desc,
            "short_exp": short_exp,
            "short_strike": float(final_short_strike),
            "long_exp": long_exp,
            "long_strike": float(long_strike) if long_strike is not None else None,
        }