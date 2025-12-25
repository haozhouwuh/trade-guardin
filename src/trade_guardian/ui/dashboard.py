import streamlit as st
import pandas as pd
import sqlite3
import os
import sys
import json
import time
import textwrap
from datetime import datetime, timedelta

# ==========================================
# 1. 环境与路径设置
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))

from trade_guardian.infra.config import load_config, DEFAULT_CONFIG
from trade_guardian.infra.schwab_client import SchwabClient
from trade_guardian.action.sniper import Sniper
from trade_guardian.app.persistence import PersistenceManager

# ==========================================
# 2. 页面配置 & CSS
# ==========================================
st.set_page_config(
    page_title="Trade Guardian",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

if "last_refresh_time" not in st.session_state:
    st.session_state.last_refresh_time = time.time()

st.markdown(
    """
<style>
    .block-container { padding-top: 1rem !important; padding-bottom: 1rem !important; }
    .stProgress > div > div > div > div { background-color: #f63366; }
    
    /* Sidebar Header */
    .sidebar-header {
        display: flex; justify-content: space-between; align-items: center;
        background-color: #262730; padding: 10px 5px; border-radius: 6px; border: 1px solid #444; margin-bottom: 15px;
    }
    .header-item { flex: 1; text-align: center; border-right: 1px solid #555; line-height: 1.2; }
    .header-item:last-child { border-right: none; }
    .header-label { font-size: 0.7rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
    .header-value { font-size: 1rem; font-weight: 700; color: #eee; white-space: nowrap; }

    /* Blueprint Box (Sidebar) */
    .blueprint-box {
        font-size: 0.85rem; background-color: #1e1e1e; border: 1px solid #333;
        border-radius: 4px; padding: 8px 12px; margin-bottom: 6px;
        display: flex; justify-content: space-between;
    }
    .leg-buy { border-left: 4px solid #00c853; }
    .leg-sell { border-left: 4px solid #f44336; }

    /* Calc Box */
    .calc-box {
        background-color: #0e1117; border: 1px solid #4caf50; border-radius: 8px;
        padding: 15px; text-align: center; margin-top: 15px; margin-bottom: 15px;
        box-shadow: 0 0 10px rgba(76, 175, 80, 0.1);
    }
    .calc-title { color: #888; font-size: 0.8rem; letter-spacing: 1px; margin-bottom: 5px; }
    .calc-price { font-size: 2.4rem; font-weight: 700; color: #4caf50; font-family: 'Roboto Mono', monospace; line-height: 1; }
    .calc-sub { font-size: 0.9rem; color: #aaa; margin-top: 5px; }

    /* Trade Manager Card */
    .trade-card {
        background-color: #1e1e1e; border: 1px solid #444; border-radius: 8px; padding: 15px; margin-bottom: 15px;
    }
    .trade-status-working { border-left: 5px solid #ffeb3b; }
    .trade-status-open { border-left: 5px solid #00c853; }
    
    /* Leg Details inside Card */
    .card-leg-row {
        display: flex; justify-content: flex-start; align-items: center; 
        font-family: monospace; font-size: 0.85rem; margin-top: 4px;
        color: #ddd;
    }
    .badge-buy { background-color: #00c853; color: black; padding: 1px 6px; border-radius: 3px; font-weight: bold; margin-right: 8px; font-size: 0.75rem; }
    .badge-sell { background-color: #f44336; color: white; padding: 1px 6px; border-radius: 3px; font-weight: bold; margin-right: 8px; font-size: 0.75rem; }
    .leg-meta { margin-right: 12px; }
    
</style>
""",
    unsafe_allow_html=True
)

# ==========================================
# 3. 辅助函数
# ==========================================

def _pick_cfg_path() -> str | None:
    candidates = [
        os.path.join(project_root, "config", "config.yaml"),
        os.path.join(project_root, "config", "config.yml"),
        os.path.join(project_root, "config", "config.json"),
    ]
    for p in candidates:
        if os.path.exists(p): return p
    return None

@st.cache_resource
def get_sniper():
    cfg_path = _pick_cfg_path()
    if not cfg_path: return None
    cfg = load_config(cfg_path, DEFAULT_CONFIG)
    return Sniper(SchwabClient(cfg))

@st.cache_resource
def get_db_manager():
    return PersistenceManager() 

def get_past_batch_id(conn, current_ts_str, minutes_ago):
    try:
        curr_dt = datetime.strptime(current_ts_str, "%Y-%m-%d %H:%M:%S")
        target_dt = curr_dt - timedelta(minutes=minutes_ago)
        target_str = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT batch_id, timestamp FROM scan_batches WHERE timestamp <= ? ORDER BY batch_id DESC LIMIT 1",
            (target_str,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None

@st.cache_data(ttl=10)
def load_radar_with_deltas():
    db_path = os.path.join(project_root, "db", "trade_guardian.db")
    if not os.path.exists(db_path):
        return None, None

    conn = sqlite3.connect(db_path)
    try:
        curr_batch = conn.execute(
            "SELECT batch_id, timestamp, market_vix FROM scan_batches ORDER BY batch_id DESC LIMIT 1"
        ).fetchone()
        if not curr_batch:
            return None, None
        curr_id, curr_ts, vix = curr_batch

        id_10m = get_past_batch_id(conn, curr_ts, 10)
        id_1h = get_past_batch_id(conn, curr_ts, 60)

        # [MODIFIED] Added s.snapshot_id for trade tracking
        query_main = """
            SELECT
                s.snapshot_id,
                s.symbol, s.price, s.iv_short, s.edge, s.regime,
                p.strategy_type, p.tag, p.cal_score, p.gate_status, p.blueprint_json,
                (p.cal_score + CASE p.gate_status
                    WHEN 'EXEC'   THEN 120
                    WHEN 'LIMIT'  THEN 40
                    WHEN 'WAIT'   THEN -40
                    WHEN 'FORBID' THEN -200
                    ELSE -60
                END) AS rank_score
            FROM market_snapshots s
            JOIN trade_plans p ON s.snapshot_id = p.snapshot_id
            WHERE s.batch_id = ?
            ORDER BY rank_score DESC, p.cal_score DESC
        """
        df = pd.read_sql_query(query_main, conn, params=(curr_id,))

        df["d_10m"] = 0.0
        df["d_1h"] = 0.0

        if id_10m:
            df_10 = pd.read_sql_query("SELECT symbol, price FROM market_snapshots WHERE batch_id = ?", conn, params=(id_10m,))
            merged = df.merge(df_10, on="symbol", how="left", suffixes=("", "_old"))
            df["d_10m"] = merged["price"] - merged["price_old"]

        if id_1h:
            df_1h = pd.read_sql_query("SELECT symbol, price FROM market_snapshots WHERE batch_id = ?", conn, params=(id_1h,))
            merged = df.merge(df_1h, on="symbol", how="left", suffixes=("", "_old"))
            df["d_1h"] = merged["price"] - merged["price_old"]

        df["d_10m"] = pd.to_numeric(df["d_10m"], errors='coerce').fillna(0.0)
        df["d_1h"] = pd.to_numeric(df["d_1h"], errors='coerce').fillna(0.0)

        return df, (curr_ts, vix)
    finally:
        conn.close()

# [MODIFIED V2] Calculate Live PnL Logic (Database Legs Support)
def calculate_live_pnl(trades, sniper_client):
    if not trades or not sniper_client:
        return trades or []
    
    enhanced_trades = []
    
    # Credit Strategies Keywords
    CREDIT_KEYWORDS = ["BULL-PUT", "BEAR-CALL", "CREDIT", "IC", "IRON", "CONDOR", "VERTICAL"]

    for t in trades:
        # t is a dict from sqlite3.Row
        t_enhanced = dict(t)
        
        # Only calculate PnL for OPEN trades
        if t['status'] == 'OPEN':
            try:
                # [NEW V2] Legs are already list of dicts from PersistenceManager
                legs = t.get('legs', [])
                
                # Check Strategy Type (Credit vs Debit)
                strat_type = str(t.get('strategy', '')).upper()
                tags = str(t.get('tags', '')).upper()
                is_credit = any(k in strat_type or k in tags for k in CREDIT_KEYWORDS)
                
                current_strategy_value = 0.0
                all_legs_valid = True
                
                # We need to update leg prices in place
                live_legs = []

                for leg in legs:
                    # Persistence returns dict, we can modify or copy
                    l_copy = dict(leg)
                    
                    # Columns in trade_legs table: exp_date, strike, op_type, action
                    exp = l_copy.get('exp_date')
                    strike = float(l_copy.get('strike'))
                    op_type = l_copy.get('op_type') # CALL/PUT
                    action = l_copy.get('action')   # BUY/SELL
                    
                    chain_data = sniper_client._fetch_chain_one_exp(t['symbol'], exp)
                    side_key = "callExpDateMap" if op_type.upper() == "CALL" else "putExpDateMap"
                    q_data = sniper_client._extract_quote(chain_data, side_key, exp, strike)
                    
                    leg_price = 0.0
                    if q_data:
                        bid = float(q_data.get('bid', 0))
                        ask = float(q_data.get('ask', 0))
                        mark = float(q_data.get('mark', 0))
                        
                        if bid > 0 and ask > 0:
                            mid = (bid + ask) / 2.0
                        elif mark > 0:
                            mid = mark
                        else:
                            mid = 0.0
                        
                        leg_price = mid
                        
                        # Market Value Contribution:
                        # Buy Leg = +Value (Asset)
                        # Sell Leg = -Value (Liability)
                        side_mult = 1 if action.upper() == 'BUY' else -1
                        current_strategy_value += (mid * side_mult)
                    else:
                        all_legs_valid = False
                        
                    # [NEW] Inject live price into leg dict
                    l_copy['live_price'] = leg_price
                    live_legs.append(l_copy)
                
                # Replace original legs with enriched legs
                t_enhanced['legs'] = live_legs

                if all_legs_valid:
                    fill_price = float(t['initial_cost'] or 0.0) # V2 field is initial_cost
                    qty = int(t['quantity'] or 1)
                    
                    # PnL Logic
                    if is_credit:
                        pnl_total = (fill_price + current_strategy_value) * 100 * qty
                        if abs(fill_price) > 0.01:
                            pnl_pct = (pnl_total / (abs(fill_price) * 100 * qty)) * 100
                        else:
                            pnl_pct = 0.0
                    else:
                        pnl_total = (current_strategy_value - fill_price) * 100 * qty
                        if abs(fill_price) > 0.01:
                            pnl_pct = (pnl_total / (fill_price * 100 * qty)) * 100
                        else:
                            pnl_pct = 0.0
                    
                    t_enhanced['live_pnl'] = pnl_total
                    t_enhanced['live_pnl_pct'] = pnl_pct
                    t_enhanced['current_val'] = current_strategy_value
                else:
                    t_enhanced['live_pnl'] = None 
                    
            except Exception as e:
                # print(f"PnL Error: {e}")
                t_enhanced['live_pnl'] = None
        
        enhanced_trades.append(t_enhanced)
        
    return enhanced_trades

# ==========================================
# 4. 主 UI 逻辑
# ==========================================

st.title("🛡️ Trade Guardian Command Center")

# Header VIX Display
db_path = os.path.join(project_root, "db", "trade_guardian.db")
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    curr_batch = conn.execute("SELECT timestamp, market_vix FROM scan_batches ORDER BY batch_id DESC LIMIT 1").fetchone()
    conn.close()
    
    if curr_batch:
        ts, vix = curr_batch
        vix_val = float(vix)
        vix_label = "NORMAL"
        vix_color = "#ffd700"
        if vix_val < 15:
            vix_color = "#00c853"; vix_label = "LOW"
        elif 20 <= vix_val < 25:
            vix_color = "#ff9800"; vix_label = "ELEVATED"
        elif vix_val >= 25:
            vix_color = "#f44336"; vix_label = "PANIC"

        col1, col2, col3, col4 = st.columns([2, 1.5, 1.5, 1])
        col1.markdown(f"""
            <div style="background-color: #262730; padding: 10px; border-radius: 5px; border-left: 5px solid {vix_color};">
                <div style="font-size: 0.8rem; color: #aaa;">MARKET VIX</div>
                <div style="font-size: 1.5rem; font-weight: bold; color: white;">{vix:.2f} <span style="font-size:0.8rem; color:{vix_color}">({vix_label})</span></div>
            </div>
            """, unsafe_allow_html=True)
        col2.metric("Last Scan", ts.split(" ")[1])
        if col4.button("🔄 Refresh All"):
            load_radar_with_deltas.clear()
            st.rerun()

# -----------------
# Tabs
# -----------------
tab_scanner, tab_manager = st.tabs(["📡 Scanner (Radar)", "💼 Active Trades (Manager)"])

# ==========================================
# TAB 1: Scanner (Radar)
# ==========================================
with tab_scanner:
    if "auto_refresh" not in st.session_state:
        st.session_state.auto_refresh = True

    df, metadata = load_radar_with_deltas()
    
    if df is not None:
        display_df = df.copy()
        
        def format_delta(val):
            if val > 0: return f"🟢 +{val:.2f}"
            elif val < 0: return f"🔴 {val:.2f}"
            else: return f"⚪ {val:.2f}"

        display_df["Δ10m"] = display_df["d_10m"].apply(format_delta)
        display_df["Δ1h"] = display_df["d_1h"].apply(format_delta)

        cols = ["symbol", "price", "Δ10m", "Δ1h", "iv_short", "edge", "regime", "strategy_type", "tag", "gate_status", "cal_score", "blueprint_json"]
        display_df = display_df[[c for c in cols if c in display_df.columns]]

        column_cfg = {
            "blueprint_json": None,
            "cal_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
            "symbol": st.column_config.TextColumn("Symbol", width="small"),
            "price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "edge": st.column_config.NumberColumn("Edge", format="%.2f"),
            "iv_short": st.column_config.NumberColumn("IV", format="%.1f%%"),
        }

        # Main Table
        event = st.dataframe(
            display_df, width="stretch", hide_index=True, column_config=column_cfg,
            selection_mode="single-row", on_select="rerun", height=600, key="radar_table"
        )

        # --- Sidebar (Action Panel) ---
        if len(event.selection.rows) > 0:
            selected_index = event.selection.rows[0]
            row = df.iloc[selected_index]
            symbol = row["symbol"]
            bp_json_raw = row["blueprint_json"]
            snapshot_id = int(row.get("snapshot_id", 0))

            with st.sidebar:
                st.markdown(f"## 🔭 Scope: **{symbol}**")
                
                gate_color = "#00c853" if row["gate_status"] == "EXEC" else ("#ffeb3b" if row["gate_status"] == "LIMIT" else "#f44336")
                st.markdown(f"""
<div class="sidebar-header">
<div class="header-item"><div class="header-label">STRATEGY</div><div class="header-value">{row['strategy_type']}</div></div>
<div class="header-item"><div class="header-label">TAG</div><div class="header-value">{row['tag']}</div></div>
<div class="header-item"><div class="header-label">GATE</div><div class="header-value" style="color: {gate_color};">{row['gate_status']}</div></div>
</div>
""", unsafe_allow_html=True)

                bp_valid = False
                short_exp, short_strike = None, 0.0
                long_exp, long_strike = None, 0.0
                target_strategy = "STRADDLE"

                if bp_json_raw:
                    try:
                        bp_data = json.loads(bp_json_raw)
                        legs = bp_data.get("legs", [])
                        if legs:
                            bp_valid = True
                            strat_name = str(row["strategy_type"]).upper()
                            tag_name = str(row["tag"]).upper()
                            multi_leg_kws = ["DIAGONAL", "PMCC", "BULL", "BEAR", "VERT", "PCS", "CCS", "IC", "IRON", "CONDOR"]
                            is_multi_leg = any(k in strat_name or k in tag_name for k in multi_leg_kws)

                            if is_multi_leg:
                                target_strategy = row["strategy_type"]
                                for leg in legs:
                                    cls = "leg-buy" if leg["action"] == "BUY" else "leg-sell"
                                    icon = "🟢" if leg["action"] == "BUY" else "🔴"
                                    # [FIXED HTML INDENT]
                                    st.markdown(f"""<div class="blueprint-box {cls}"><span>{icon} <b>{leg['action']} {leg['ratio']}x</b></span><span>{leg['exp']}</span><span><b>{leg['strike']} {leg['type']}</b></span></div>""", unsafe_allow_html=True)
                                    if leg["action"] == "SELL": short_exp = leg["exp"]; short_strike = float(leg["strike"])
                                    elif leg["action"] == "BUY": long_exp = leg["exp"]; long_strike = float(leg["strike"])
                            else:
                                target_strategy = "STRADDLE"
                                for leg in legs:
                                    cls = "leg-buy" if leg["action"] == "BUY" else "leg-sell"
                                    # [FIXED HTML INDENT]
                                    st.markdown(f"""<div class="blueprint-box {cls}"><span>{leg['action']} {leg['ratio']}x</span><span>{leg['exp']}</span><span><b>{leg['strike']} {leg['type']}</b></span></div>""", unsafe_allow_html=True)
                                if legs: short_exp = legs[0]["exp"]; short_strike = float(legs[0]["strike"])
                    except Exception as e:
                        st.error(f"Blueprint Error: {e}")

                st.divider()
                urgency = st.radio("Pricing Mode", ["PASSIVE", "NEUTRAL", "AGGRESSIVE"], horizontal=False, label_visibility="collapsed")
                limit_price_display, est_cost_display, is_ready, limit_price_val = "---", "---", False, 0.0

                if bp_valid and short_exp:
                    try:
                        sniper = get_sniper()
                        if sniper:
                            res = sniper.lock_target(
                                symbol=symbol, strategy=target_strategy, short_exp=short_exp, short_strike=short_strike,
                                long_exp=long_exp, long_strike=long_strike, urgency=urgency
                            )
                            if res.get("status") == "READY":
                                is_ready = True
                                limit_price_val = res['limit_price']
                                limit_price_display = f"${limit_price_val:.2f}"
                                est_cost_display = f"${res['est_cost']:.0f}"
                            else:
                                st.error(res.get("msg", "Sniper FAIL"))
                        else:
                            st.error("Sniper not initialized.")
                    except Exception as e:
                        st.error(f"Pricing Error: {e}")

                # [FIXED HTML INDENT]
                st.markdown(f"""<div class="calc-box"><div class="calc-title">CALCULATED LIMIT ({urgency})</div><div class="calc-price">{limit_price_display}</div><div class="calc-sub">Est. Cost: {est_cost_display}</div></div>""", unsafe_allow_html=True)

                if st.button("💾 RECORD ORDER (DB)", type="primary", use_container_width=True, disabled=not is_ready):
                    db = get_db_manager()
                    trade_id = db.record_order(
                        snapshot_id=snapshot_id, symbol=symbol, strategy=target_strategy,
                        limit_price=limit_price_val, quantity=1, blueprint_json=bp_json_raw,
                        tags=row['tag'], underlying_price=float(row['price']), iv=float(row['iv_short'])
                    )
                    if trade_id: st.toast(f"✅ Order Recorded! ID: {trade_id}", icon="💾"); time.sleep(1); st.rerun()
                    else: st.error("DB Save Failed")
        else:
            with st.sidebar:
                st.info("👈 Select a target from the radar.")
                st.markdown("---")
                auto_ref = st.checkbox("Auto Refresh (5min)", value=st.session_state.auto_refresh)
                st.session_state.auto_refresh = auto_ref
    else:
        st.warning("⚠️ No scan data found.")

# ==========================================
# TAB 2: Active Trades (Manager)
# ==========================================
with tab_manager:
    st.markdown("### 💼 Active Portfolio")
    
    col_k1, col_k2, col_k3 = st.columns(3)
    
    db = get_db_manager()
    trades = db.fetch_active_trades()
    
    if not trades:
        st.info("No active trades found. Go to 'Scanner' to fire some orders!")
    else:
        # PnL Calculation
        sniper_instance = get_sniper()
        enhanced_trades = calculate_live_pnl(trades, sniper_instance)
        
        # Summary Metrics
        open_count = sum(1 for t in enhanced_trades if t['status'] == 'OPEN')
        working_count = sum(1 for t in enhanced_trades if t['status'] == 'WORKING')
        total_pnl = sum(t.get('live_pnl', 0.0) or 0.0 for t in enhanced_trades)
        
        col_k1.metric("Open Positions", open_count)
        col_k2.metric("Working Orders", working_count)
        pnl_color = "normal" if total_pnl >= 0 else "inverse"
        col_k3.metric("Unrealized PnL", f"${total_pnl:.2f}", delta=f"{total_pnl:.2f}", delta_color=pnl_color)
        
        st.divider()

        for t in enhanced_trades:
            t_id = t["trade_id"]
            sym = t["symbol"]
            strat = t["strategy"]
            status = t["status"]
            limit = t.get("initial_cost", 0.0) # V2 field
            created = t["created_at"]
            
            # [FIXED DATE FORMAT] YYYY/MM/DD HH:MM
            try:
                dt_obj = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                created_display = dt_obj.strftime("%Y/%m/%d %H:%M")
            except:
                created_display = created

            # --- CARD CONTAINER ---
            with st.container():
                # 上半部分：Status Bar (Header)
                status_color = "#ffeb3b" if status == "WORKING" else "#00c853"
                border_color = status_color
                
                # Header HTML (Minimal, no indentation)
                header_html = f"""
<div style="background-color: #1e1e1e; border-left: 5px solid {border_color}; border-radius: 4px; padding: 12px; margin-bottom: 8px; border: 1px solid #333;">
<div style="display:flex; justify-content:space-between; align-items:center;">
<div><span style="font-size:1.1rem; font-weight:bold; color:white;">{sym}</span> <span style="color:#888; font-size:0.9rem; margin-left:8px;">{strat}</span></div>
<div style="text-align:right;"><span style="color:{status_color}; font-weight:bold;">{status}</span></div>
</div>
</div>
"""
                st.markdown(header_html, unsafe_allow_html=True)
                
                # 中间部分：PnL & Price (使用原生 Columns)
                c_pnl, c_price, c_id = st.columns([2, 2, 2])
                
                with c_pnl:
                    if status == "OPEN":
                        pnl = t.get('live_pnl', 0.0)
                        pct = t.get('live_pnl_pct', 0.0)
                        st.metric("PnL", f"${pnl:.2f}", f"{pct:.1f}%")
                    else:
                        st.caption("PnL: N/A (Working)")
                
                with c_price:
                    # In V2, initial_cost IS the limit/fill price
                    fill_val = float(limit)
                    label = "Fill Price" if status == "OPEN" else "Limit Price"
                    st.metric(label, f"${fill_val:.2f}")
                    
                with c_id:
                    st.caption(f"ID: {t_id}")
                    # [FIXED DATE DISPLAY]
                    st.caption(f"Created: {created_display}")

                # 下半部分：Legs Detail (原生渲染)
                # [NEW] Enhanced Legs Detail with Live Price (V2 DB Structure)
                with st.expander("Legs Detail", expanded=True):
                    legs_list = t.get('legs', [])
                    
                    if legs_list:
                        for leg in legs_list:
                            act = str(leg.get('action', '')).upper()
                            ratio = leg.get('ratio')
                            exp = leg.get('exp_date')
                            strike = leg.get('strike')
                            op_type = leg.get('op_type')
                            live_px = leg.get('live_price')
                            
                            # [NEW] 3-Column Layout: Icon | Details | Live Price
                            col_icon, col_txt, col_px = st.columns([0.5, 6, 2])
                            
                            with col_icon:
                                st.write("🟢" if act == "BUY" else "🔴")
                            with col_txt:
                                st.markdown(f"**{act} {ratio}x** {exp} **{strike} {op_type}**")
                            with col_px:
                                if live_px is not None:
                                    st.markdown(f"<span style='color:#ffd700; font-family:monospace;'>Mkt: ${live_px:.2f}</span>", unsafe_allow_html=True)
                                else:
                                    st.caption("-")
                    else:
                        st.write("Details unavailable")

                # 底部：操作按钮 (4列布局)
                c_input, c_confirm, c_cancel, c_space = st.columns([0.8, 0.8, 0.8, 2])
                
                if status == "WORKING":
                    with c_input:
                        fill_price = st.number_input("Fill Px", value=float(limit), key=f"fp_{t_id}", label_visibility="collapsed")
                    with c_confirm:
                        if st.button("Confirm Fill", key=f"btn_fill_{t_id}", use_container_width=True):
                            db.update_trade_status(t_id, "OPEN", fill_price)
                            st.rerun()
                    with c_cancel:
                        if st.button("Cancel Order", key=f"btn_cancel_{t_id}", use_container_width=True):
                            db.update_trade_status(t_id, "CLOSED")
                            st.rerun()
                elif status == "OPEN":
                    with c_input:
                        # Use the first column slot for the Close button for alignment
                        if st.button("Close Trade", key=f"btn_close_{t_id}", use_container_width=True):
                            db.update_trade_status(t_id, "CLOSED")
                            st.rerun()
                
                st.divider()

# -----------------
# Auto Refresh Logic
# -----------------
if st.session_state.auto_refresh:
    time_elapsed = time.time() - st.session_state.last_refresh_time
    if time_elapsed > 300:
        load_radar_with_deltas.clear()
        st.session_state.last_refresh_time = time.time()
        st.rerun()