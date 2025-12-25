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
# 2. 页面配置 & CSS (Compact Mode)
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
    /* Global Compact */
    .block-container { padding-top: 0.5rem !important; padding-bottom: 1rem !important; }
    
    /* Compact Sidebar */
    .sidebar-header {
        display: flex; justify-content: space-between; align-items: center;
        background-color: #262730; padding: 6px 4px; border-radius: 4px; border: 1px solid #444; margin-bottom: 10px;
    }
    .header-item { flex: 1; text-align: center; border-right: 1px solid #555; line-height: 1.1; }
    .header-item:last-child { border-right: none; }
    .header-label { font-size: 0.65rem; color: #aaa; text-transform: uppercase; margin-bottom: 1px; }
    .header-value { font-size: 0.85rem; font-weight: 700; color: #eee; }

    /* Compact Blueprint Box */
    .blueprint-box {
        font-size: 0.75rem; background-color: #1e1e1e; border: 1px solid #333;
        border-radius: 3px; padding: 4px 8px; margin-bottom: 4px;
        display: flex; justify-content: space-between;
    }
    .leg-buy { border-left: 3px solid #00c853; }
    .leg-sell { border-left: 3px solid #f44336; }

    /* Compact Calc Box */
    .calc-box {
        background-color: #0e1117; border: 1px solid #4caf50; border-radius: 6px;
        padding: 8px; text-align: center; margin-top: 10px; margin-bottom: 10px;
    }
    .calc-title { color: #888; font-size: 0.7rem; margin-bottom: 2px; }
    .calc-price { font-size: 1.8rem; font-weight: 700; color: #4caf50; font-family: 'Roboto Mono', monospace; line-height: 1; }
    .calc-sub { font-size: 0.8rem; color: #aaa; margin-top: 2px; }

    /* === Compact Trade Card Styles === */
    .compact-card {
        background-color: #161616; border: 1px solid #333; border-radius: 4px; 
        margin-bottom: 8px; padding: 0; overflow: hidden;
    }
    
    /* Status Strip */
    .status-strip-working { border-left: 4px solid #ffeb3b; }
    .status-strip-open { border-left: 4px solid #00c853; }
    .status-strip-closed { border-left: 4px solid #9e9e9e; }

    /* Header Row */
    .card-header {
        display: flex; justify-content: space-between; align-items: center;
        padding: 6px 12px; background-color: #1e1e1e; border-bottom: 1px solid #2a2a2a;
    }
    .sym-box { display: flex; align-items: baseline; gap: 8px; }
    .sym-text { font-size: 1rem; font-weight: 700; color: #eee; }
    .strat-text { font-size: 0.75rem; color: #888; background: #2a2a2a; padding: 1px 5px; border-radius: 3px; }
    .id-text { font-size: 0.7rem; color: #555; margin-left: 8px; }
    
    .metrics-box { display: flex; align-items: center; gap: 15px; }
    .metric-item { text-align: right; line-height: 1.1; }
    .metric-val { font-size: 0.95rem; font-weight: 700; color: #ddd; font-family: 'Roboto Mono', monospace; }
    .metric-lbl { font-size: 0.65rem; color: #777; }
    .pnl-pos { color: #00c853; }
    .pnl-neg { color: #f44336; }
    .t-pnl-neutral { color: #777; font-family: 'Roboto Mono', monospace; font-size: 0.95rem; }

    .status-badge {
        padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.7rem; text-transform: uppercase;
    }

    /* Legs Section */
    .legs-container { padding: 4px 12px; background-color: #161616; }
    .leg-row {
        display: flex; justify-content: flex-start; align-items: center;
        padding: 2px 0; font-size: 0.8rem; font-family: 'Roboto Mono', monospace; color: #bbb;
    }
    .leg-icon { font-size: 0.6rem; margin-right: 6px; width: 12px; text-align: center; }
    .leg-desc { flex: 1; }
    .leg-price { width: 80px; text-align: right; color: #888; font-size: 0.75rem; }
    .leg-pnl { width: 80px; text-align: right; font-weight: bold; font-size: 0.75rem; }

    /* Action Bar */
    .action-bar { padding: 4px 12px 8px 12px; }
    
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

def calculate_live_pnl(trades, sniper_client):
    if not trades or not sniper_client:
        return trades or []
    enhanced_trades = []
    CREDIT_KEYWORDS = ["BULL-PUT", "BEAR-CALL", "CREDIT", "IC", "IRON", "CONDOR", "VERTICAL"]
    for t in trades:
        t_enhanced = dict(t)
        if t['status'] == 'OPEN':
            try:
                legs = t.get('legs', [])
                strat_type = str(t.get('strategy', '')).upper()
                tags = str(t.get('tags', '')).upper()
                is_credit = any(k in strat_type or k in tags for k in CREDIT_KEYWORDS)
                current_strategy_value = 0.0
                all_legs_valid = True
                live_legs = []
                for leg in legs:
                    l_copy = dict(leg)
                    exp = l_copy.get('exp_date')
                    strike = float(l_copy.get('strike'))
                    op_type = l_copy.get('op_type') 
                    action = l_copy.get('action')   
                    entry_px = float(l_copy.get('entry_price', 0.0) or 0.0)
                    chain_data = sniper_client._fetch_chain_one_exp(t['symbol'], exp)
                    side_key = "callExpDateMap" if op_type.upper() == "CALL" else "putExpDateMap"
                    q_data = sniper_client._extract_quote(chain_data, side_key, exp, strike)
                    leg_price = 0.0
                    if q_data:
                        bid = float(q_data.get('bid', 0))
                        ask = float(q_data.get('ask', 0))
                        mark = float(q_data.get('mark', 0))
                        if bid > 0 and ask > 0: mid = (bid + ask) / 2.0
                        elif mark > 0: mid = mark
                        else: mid = 0.0
                        leg_price = mid
                        side_mult = 1 if action.upper() == 'BUY' else -1
                        current_strategy_value += (mid * side_mult)
                        if t['status'] == 'OPEN' and entry_px > 0:
                            if action.upper() == 'BUY': l_pnl = (leg_price - entry_px) * 100 * int(t.get('quantity', 1))
                            else: l_pnl = (entry_px - leg_price) * 100 * int(t.get('quantity', 1))
                            l_copy['leg_pnl'] = l_pnl
                        else: l_copy['leg_pnl'] = None
                    else: all_legs_valid = False
                    l_copy['live_price'] = leg_price
                    live_legs.append(l_copy)
                t_enhanced['legs'] = live_legs
                if t['status'] == 'OPEN' and all_legs_valid:
                    fill_price = float(t['initial_cost'] or 0.0)
                    qty = int(t['quantity'] or 1)
                    if is_credit:
                        pnl_total = (fill_price + current_strategy_value) * 100 * qty
                        pnl_pct = (pnl_total / (abs(fill_price) * 100 * qty)) * 100 if abs(fill_price)>0.01 else 0.0
                    else:
                        pnl_total = (current_strategy_value - fill_price) * 100 * qty
                        pnl_pct = (pnl_total / (fill_price * 100 * qty)) * 100 if abs(fill_price)>0.01 else 0.0
                    t_enhanced['live_pnl'] = pnl_total
                    t_enhanced['live_pnl_pct'] = pnl_pct
                    t_enhanced['current_val'] = current_strategy_value
                else: t_enhanced['live_pnl'] = None 
            except Exception as e: t_enhanced['live_pnl'] = None
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

        c1, c2, c3, c4 = st.columns([1.5, 1, 1, 1])
        
        # [MODIFIED] Large VIX
        with c1:
            st.markdown(f"""
                <div style="background-color: #262730; padding: 8px 12px; border-radius: 5px; border-left: 5px solid {vix_color};">
                    <div style="font-size: 0.75rem; color: #aaa; text-transform: uppercase;">Market VIX</div>
                    <div style="font-size: 2.0rem; font-weight: bold; color: white; line-height: 1;">{vix:.2f} <span style="font-size:0.9rem; color:{vix_color}">({vix_label})</span></div>
                </div>
            """, unsafe_allow_html=True)
            
        # [MODIFIED] Small Last Scan
        with c2:
            st.caption("Last Scan")
            st.markdown(f"<span style='font-size: 1.1rem; font-weight: bold;'>{ts.split(' ')[1]}</span>", unsafe_allow_html=True)

        with c4:
            if st.button("🔄"):
                load_radar_with_deltas.clear()
                st.rerun()

tab_scanner, tab_manager = st.tabs(["📡 Scanner", "💼 Active Trades"])

# ==========================================
# TAB 1: Scanner (保持不变，只是 Sidebar 微调)
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
            "symbol": st.column_config.TextColumn("Sym", width="small"),
            "price": st.column_config.NumberColumn("Px", format="$%.2f"),
            "edge": st.column_config.NumberColumn("Edge", format="%.2f"),
            "iv_short": st.column_config.NumberColumn("IV", format="%.1f%%"),
        }

        event = st.dataframe(
            display_df, width="stretch", hide_index=True, column_config=column_cfg,
            selection_mode="single-row", on_select="rerun", height=600, key="radar_table"
        )

        if len(event.selection.rows) > 0:
            selected_index = event.selection.rows[0]
            row = df.iloc[selected_index]
            symbol = row["symbol"]
            bp_json_raw = row["blueprint_json"]
            snapshot_id = int(row.get("snapshot_id", 0))

            with st.sidebar:
                st.markdown(f"#### 🔭 {symbol}")
                gate_color = "#00c853" if row["gate_status"] == "EXEC" else ("#ffeb3b" if row["gate_status"] == "LIMIT" else "#f44336")
                st.markdown(f"""
<div class="sidebar-header">
<div class="header-item"><div class="header-label">TYPE</div><div class="header-value">{row['strategy_type'][:8]}</div></div>
<div class="header-item"><div class="header-label">TAG</div><div class="header-value">{row['tag']}</div></div>
<div class="header-item"><div class="header-label">GATE</div><div class="header-value" style="color: {gate_color};">{row['gate_status']}</div></div>
</div>
""", unsafe_allow_html=True)

                bp_valid = False
                short_exp, short_strike, long_exp, long_strike = None, 0.0, None, 0.0
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
                                    icon = "B" if leg["action"] == "BUY" else "S"
                                    st.markdown(f"""<div class="blueprint-box {cls}"><span><b>{icon} {leg['ratio']}x</b> {leg['exp']}</span><span>{leg['strike']} {leg['type']}</span></div>""", unsafe_allow_html=True)
                                    if leg["action"] == "SELL": short_exp = leg["exp"]; short_strike = float(leg["strike"])
                                    elif leg["action"] == "BUY": long_exp = leg["exp"]; long_strike = float(leg["strike"])
                            else:
                                target_strategy = "STRADDLE"
                                for leg in legs:
                                    cls = "leg-buy" if leg["action"] == "BUY" else "leg-sell"
                                    st.markdown(f"""<div class="blueprint-box {cls}"><span><b>{leg['action']}</b> {leg['exp']}</span><span>{leg['strike']} {leg['type']}</span></div>""", unsafe_allow_html=True)
                                if legs: short_exp = legs[0]["exp"]; short_strike = float(legs[0]["strike"])
                    except: st.error("Blueprint Error")

                st.divider()
                urgency = st.radio("Pricing", ["PASSIVE", "NEUTRAL", "AGGRESSIVE"], horizontal=True, label_visibility="collapsed")
                limit_price_display, est_cost_display, is_ready, limit_price_val = "---", "---", False, 0.0

                if bp_valid and short_exp:
                    try:
                        sniper = get_sniper()
                        if sniper:
                            res = sniper.lock_target(symbol, target_strategy, short_exp, short_strike, long_exp, long_strike, urgency)
                            if res.get("status") == "READY":
                                is_ready = True
                                limit_price_val = res['limit_price']
                                limit_price_display = f"${limit_price_val:.2f}"
                                est_cost_display = f"${res['est_cost']:.0f}"
                            else: st.error(res.get("msg", "Sniper FAIL"))
                    except: st.error("Pricing Error")

                st.markdown(f"""<div class="calc-box"><div class="calc-title">LIMIT ({urgency})</div><div class="calc-price">{limit_price_display}</div><div class="calc-sub">Est: {est_cost_display}</div></div>""", unsafe_allow_html=True)

                if st.button("💾 RECORD (DB)", type="primary", use_container_width=True, disabled=not is_ready):
                    db = get_db_manager()
                    trade_id = db.record_order(
                        snapshot_id=snapshot_id, symbol=symbol, strategy=target_strategy,
                        limit_price=limit_price_val, quantity=1, blueprint_json=bp_json_raw,
                        tags=row['tag'], underlying_price=float(row['price']), iv=float(row['iv_short'])
                    )
                    if trade_id: st.toast(f"Recorded! ID: {trade_id}", icon="💾"); time.sleep(1); st.rerun()
        else:
            with st.sidebar:
                st.info("👈 Select target")
                auto_ref = st.checkbox("Auto-Refresh", value=st.session_state.auto_refresh)
                st.session_state.auto_refresh = auto_ref
    else:
        st.warning("⚠️ No scan data found.")

# ==========================================
# TAB 2: Active Trades (COMPACT MODE)
# ==========================================
with tab_manager:
    db = get_db_manager()
    trades = db.fetch_active_trades()
    
    if not trades:
        st.caption("No active trades.")
    else:
        sniper_instance = get_sniper()
        enhanced_trades = calculate_live_pnl(trades, sniper_instance)
        
        # [MODIFIED] Top Stats Row (Compact & Right-aligned PnL)
        open_c = sum(1 for t in enhanced_trades if t['status']=='OPEN')
        work_c = sum(1 for t in enhanced_trades if t['status']=='WORKING')
        tot_pnl = sum(t.get('live_pnl', 0.0) or 0.0 for t in enhanced_trades)
        
        c1, c2, c3, c4 = st.columns([0.8, 0.8, 2, 2])
        
        with c1:
            st.markdown(f"<div style='font-size:0.8rem; color:#aaa'>Open</div><div style='font-size:1.1rem; font-weight:bold; color:#fff'>{open_c}</div>", unsafe_allow_html=True)
            
        with c2:
            st.markdown(f"<div style='font-size:0.8rem; color:#aaa'>Working</div><div style='font-size:1.1rem; font-weight:bold; color:#ffeb3b'>{work_c}</div>", unsafe_allow_html=True)
            
        with c3:
            p_color = "#00c853" if tot_pnl >= 0 else "#f44336"
            p_delta = f"+{tot_pnl:.2f}" if tot_pnl >= 0 else f"{tot_pnl:.2f}"
            st.markdown(f"""
                <div style='font-size:0.8rem; color:#aaa'>Unrealized PnL</div>
                <div style='display:flex; align-items:baseline; gap:8px'>
                    <span style='font-size:1.1rem; font-weight:bold; color:#fff'>${tot_pnl:.2f}</span>
                    <span style='font-size:0.85rem; color:{p_color}; background:#1e1e1e; padding:1px 4px; border-radius:3px'>{p_delta}</span>
                </div>
            """, unsafe_allow_html=True)
        
        st.markdown("---")

        # Compact Rows
        for t in enhanced_trades:
            t_id = t["trade_id"]
            sym = t["symbol"]
            strat = t["strategy"]
            status = t["status"]
            limit = float(t.get("initial_cost", 0.0))
            created = t["created_at"]
            
            try:
                dt_obj = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                created_display = dt_obj.strftime("%m/%d %H:%M")
            except: created_display = created

            # Prepare PnL HTML string
            if status == "OPEN":
                pnl = t.get('live_pnl', 0.0)
                pct = t.get('live_pnl_pct', 0.0)
                cls = "t-pnl-pos" if pnl >= 0 else "t-pnl-neg"
                pnl_html = f"<span class='{cls}'>${pnl:.0f} ({pct:.1f}%)</span>"
            else:
                pnl_html = "<span class='t-pnl-neutral'>--</span>"

            status_border = "border-working" if status=="WORKING" else "border-open"
            status_badge_cls = "st-working" if status=="WORKING" else "st-open"

            # 1. Compact Header Row (HTML)
            header_html = f"""
            <div class="trade-row {status_border}">
                <div>
                    <span class="t-sym">{sym}</span>
                    <span class="t-strat">{strat}</span>
                    <span class="t-meta">#{t_id} {created_display}</span>
                </div>
                <div class="t-right">
                    {pnl_html}
                    <span class="t-price">${limit:.2f}</span>
                    <span class="t-status {status_badge_cls}">{status}</span>
                </div>
            </div>
            """
            st.markdown(textwrap.dedent(header_html), unsafe_allow_html=True)
            
            # 2. Hidden Details (Expander)
            with st.expander("Manage / Details"):
                # Legs Table
                legs_list = t.get('legs', [])
                if legs_list:
                    for leg in legs_list:
                        act = str(leg.get('action', '')).upper()
                        ratio = leg.get('ratio')
                        exp = leg.get('exp_date')
                        strike = leg.get('strike')
                        op_type = leg.get('op_type')
                        live_px = leg.get('live_price')
                        leg_pnl = leg.get('leg_pnl')
                        
                        icon = "🟢" if act == "BUY" else "🔴"
                        c_a, c_b, c_c, c_d = st.columns([0.5, 5, 2, 2])
                        c_a.write(icon)
                        c_b.caption(f"**{act} {ratio}x** {exp} **{strike} {op_type}**")
                        if live_px is not None: c_c.caption(f"Mkt ${live_px:.2f}")
                        if leg_pnl is not None: 
                            p_col = "green" if leg_pnl >=0 else "red"
                            c_d.markdown(f":{p_col}[${leg_pnl:.0f}]")
                
                # Buttons
                c_act1, c_act2, c_act3, c_act4 = st.columns([1, 1, 3, 1])
                if status == "WORKING":
                    with c_act1:
                        fill_px = st.number_input("Fill Px", value=limit, key=f"f_{t_id}", label_visibility="collapsed")
                    with c_act2:
                        if st.button("Fill", key=f"b_fill_{t_id}"):
                            db.update_trade_status(t_id, "OPEN", fill_px)
                            if 'legs' in t: db.update_leg_entry_prices(t_id, t['legs'])
                            st.rerun()
                    with c_act4:
                        if st.button("Cancel", key=f"b_can_{t_id}"):
                            db.update_trade_status(t_id, "CLOSED"); st.rerun()
                elif status == "OPEN":
                    with c_act4:
                        if st.button("Close", key=f"b_cls_{t_id}"):
                            db.update_trade_status(t_id, "CLOSED"); st.rerun()
            
            # Tiny spacer between cards
            st.markdown("<div style='margin-bottom: 8px;'></div>", unsafe_allow_html=True)

if st.session_state.auto_refresh and time.time() - st.session_state.last_refresh_time > 300:
    load_radar_with_deltas.clear()
    st.session_state.last_refresh_time = time.time()
    st.rerun()