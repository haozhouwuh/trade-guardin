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
        """
        支持 Straddle 和 Diagonal
        ✅ 对齐你的 SchwabClient：只用 _fetch_chain()
        ✅ 更严格：任一腿 bid/ask <=0 直接 FAIL（否则 UI 会一直 ---）
        """
        strat = (strategy or "").strip().upper()
        urg = (urgency or "PASSIVE").strip().upper()

        print(f"\n🔭 {Fore.CYAN}SNIPER: Locking Target for {symbol} ({strat})...{Style.RESET_ALL}")

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

        # A) STRADDLE / LG
        if strat in {"STRADDLE", "LG", "LONG_GAMMA", "AUTO-LG", "AUTO_LG"}:
            chain_data = self._fetch_chain_one_exp(symbol=symbol, exp=short_exp)
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
                return {"status": "FAIL", "msg": "Zero Liquidity (Straddle legs bid/ask)"}

            bid = q_call["bid"] + q_put["bid"]
            ask = q_call["ask"] + q_put["ask"]
            comp_mid = q_call["mid"] + q_put["mid"]
            legs_desc = f"+{short_exp} {final_short_strike}C +{short_exp} {final_short_strike}P"

        # B) DIAGONAL / PMCC
        elif strat in {"DIAGONAL", "PMCC", "AUTO-DIAG", "AUTO_DIAG"}:
            if not long_exp or long_strike is None:
                return {"status": "FAIL", "msg": "Diagonal requires long_exp and long_strike"}

            chain_short = self._fetch_chain_one_exp(symbol=symbol, exp=short_exp)
            chain_long = self._fetch_chain_one_exp(symbol=symbol, exp=long_exp)

            q_short_raw = self._extract_quote(chain_short, "callExpDateMap", short_exp, float(short_strike))
            q_long_raw = self._extract_quote(chain_long, "callExpDateMap", long_exp, float(long_strike))

            if not q_short_raw or not q_long_raw:
                missing = []
                if not q_short_raw:
                    missing.append(f"Short({short_exp} {short_strike})")
                if not q_long_raw:
                    missing.append(f"Long({long_exp} {long_strike})")
                return {"status": "FAIL", "msg": f"Missing Diagonal Quotes: {', '.join(missing)}"}

            q_short = _norm_quote(q_short_raw)
            q_long = _norm_quote(q_long_raw)

            if q_short["bid"] <= 0 or q_short["ask"] <= 0 or q_long["bid"] <= 0 or q_long["ask"] <= 0:
                return {"status": "FAIL", "msg": "Zero Liquidity (Diagonal legs bid/ask)"}

            bid = q_long["bid"] - q_short["ask"]
            ask = q_long["ask"] - q_short["bid"]
            comp_mid = q_long["mid"] - q_short["mid"]

            legs_desc = f"+{long_exp} {float(long_strike)}C / -{short_exp} {float(short_strike)}C"

        else:
            return {"status": "FAIL", "msg": f"Unknown Strategy: {strategy}"}

        if comp_mid <= 0 and bid > 0 and ask > 0:
            comp_mid = (bid + ask) / 2.0

        comp_spread = ask - bid

        safe_res = safety.check_liquidity({"bid": bid, "ask": ask})
        if not safe_res.passed:
            print(f"   • {Fore.RED}SAFETY BLOCK: {safe_res.reason}{Style.RESET_ALL}")
            return {"status": "FAIL", "msg": safe_res.reason}

        if comp_mid <= 0:
            return {"status": "FAIL", "msg": "Zero Mid Price (Composite)"}

        print(f"   • Liquidity: OK (Spread: {comp_spread:.2f})")

        tick = self._get_tick_size(comp_mid)

        if urg == "AGGRESSIVE":
            target_price = ask
            desc = "AGGRESSIVE (Hit Ask)"
            color = Fore.RED
        elif urg == "NEUTRAL":
            target_price = comp_mid
            desc = "NEUTRAL (Fair Value)"
            color = Fore.YELLOW
        else:
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
