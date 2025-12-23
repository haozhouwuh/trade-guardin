import streamlit as st
import pandas as pd
import sqlite3
import os
import sys
import json
import time
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

# ==========================================
# 2. 页面配置 & CSS
# ==========================================
st.set_page_config(
    page_title="Trade Guardian", 
    page_icon="🛡️", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    /* 侧边栏宽度强行锁定 450px */
    [data-testid="stSidebar"] { min-width: 450px !important; max-width: 450px !important; }
    
    /* 进度条颜色 */
    .stProgress > div > div > div > div { background-color: #f63366; }
    
    /* 侧边栏头部信息条 */
    .sidebar-header {
        display: flex; justify-content: space-between; align-items: center;
        background-color: #262730; padding: 10px 5px; border-radius: 6px; border: 1px solid #444; margin-bottom: 15px;
    }
    .header-item { flex: 1; text-align: center; border-right: 1px solid #555; line-height: 1.2; }
    .header-item:last-child { border-right: none; }
    .header-label { font-size: 0.7rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
    .header-value { font-size: 1rem; font-weight: 700; color: #eee; white-space: nowrap; }

    /* 蓝图 Box */
    .blueprint-box {
        font-size: 0.85rem; background-color: #1e1e1e; border: 1px solid #333;
        border-radius: 4px; padding: 8px 12px; margin-bottom: 6px;
        display: flex; justify-content: space-between;
    }
    .leg-buy { border-left: 4px solid #00c853; }
    .leg-sell { border-left: 4px solid #f44336; }

    /* 计算结果大屏 (LCD风格) */
    .calc-box {
        background-color: #0e1117; border: 1px solid #4caf50; border-radius: 8px;
        padding: 15px; text-align: center; margin-top: 15px; margin-bottom: 15px;
        box-shadow: 0 0 10px rgba(76, 175, 80, 0.1);
    }
    .calc-title { color: #888; font-size: 0.8rem; letter-spacing: 1px; margin-bottom: 5px; }
    .calc-price { font-size: 2.4rem; font-weight: 700; color: #4caf50; font-family: 'Roboto Mono', monospace; line-height: 1; }
    .calc-sub { font-size: 0.9rem; color: #aaa; margin-top: 5px; }

    div.row-widget.stRadio > div { flex-direction: row; gap: 5px; }
    div.row-widget.stRadio > div[role="radiogroup"] > label {
        background-color: #262730; border: 1px solid #444; padding: 5px 10px;
        border-radius: 4px; flex: 1; text-align: center; justify-content: center;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 3. 核心逻辑
# ==========================================

@st.cache_resource
def get_sniper():
    cfg_path = os.path.join(project_root, "config", "config.json")
    if not os.path.exists(cfg_path): return None
    cfg = load_config(cfg_path, DEFAULT_CONFIG)
    return Sniper(SchwabClient(cfg))

def get_past_batch_id(conn, current_ts_str, minutes_ago):
    try:
        curr_dt = datetime.strptime(current_ts_str, "%Y-%m-%d %H:%M:%S")
        target_dt = curr_dt - timedelta(minutes=minutes_ago)
        target_str = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute("SELECT batch_id, timestamp FROM scan_batches WHERE timestamp <= ? ORDER BY batch_id DESC LIMIT 1", (target_str,)).fetchone()
        if row: return row[0]
        return None
    except: return None

# [核心] 加上缓存，防止点击表格时数据重载导致选中丢失
@st.cache_data(ttl=10)
def load_radar_with_deltas():
    db_path = os.path.join(project_root, "db", "trade_guardian.db")
    if not os.path.exists(db_path): return None, None

    conn = sqlite3.connect(db_path)
    try:
        curr_batch = conn.execute("SELECT batch_id, timestamp, market_vix FROM scan_batches ORDER BY batch_id DESC LIMIT 1").fetchone()
        if not curr_batch: return None, None
        curr_id, curr_ts, vix = curr_batch
        
        id_10m = get_past_batch_id(conn, curr_ts, 10)
        id_1h = get_past_batch_id(conn, curr_ts, 60)
        
        query_main = """
            SELECT s.symbol, s.price, s.iv_short, s.edge, s.regime, p.strategy_type, p.tag, p.cal_score, p.gate_status, p.blueprint_json
            FROM market_snapshots s JOIN trade_plans p ON s.snapshot_id = p.snapshot_id
            WHERE s.batch_id = ? ORDER BY p.cal_score DESC
        """
        df = pd.read_sql_query(query_main, conn, params=(curr_id,))
        
        df['d_10m'] = 0.0
        df['d_1h'] = 0.0
        
        if id_10m:
            df_10 = pd.read_sql_query("SELECT symbol, price FROM market_snapshots WHERE batch_id = ?", conn, params=(id_10m,))
            merged = df.merge(df_10, on='symbol', how='left', suffixes=('', '_old'))
            df['d_10m'] = merged['price'] - merged['price_old']
            
        if id_1h:
            df_1h = pd.read_sql_query("SELECT symbol, price FROM market_snapshots WHERE batch_id = ?", conn, params=(id_1h,))
            merged = df.merge(df_1h, on='symbol', how='left', suffixes=('', '_old'))
            df['d_1h'] = merged['price'] - merged['price_old']

        df['d_10m'] = df['d_10m'].fillna(0.0)
        df['d_1h'] = df['d_1h'].fillna(0.0)
        
        return df, (curr_ts, vix)
    finally:
        conn.close()

# ==========================================
# 4. 界面渲染
# ==========================================

st.title("🛡️ Trade Guardian Command Center")

if 'auto_refresh' not in st.session_state:
    st.session_state.auto_refresh = True

df, metadata = load_radar_with_deltas()
auto_ref = False

if df is not None:
    ts, vix = metadata
    
    vix_val = float(vix)
    vix_label = "NORMAL"; vix_color = "#ffd700" 
    if vix_val < 15: vix_color = "#00c853"; vix_label = "LOW"
    elif vix_val >= 20 and vix_val < 25: vix_color = "#ff9800"; vix_label = "ELEVATED"
    elif vix_val >= 25: vix_color = "#f44336"; vix_label = "PANIC"

    col1, col2, col3, col4, col5 = st.columns([1.5, 1.5, 1.5, 1, 1])
    
    col1.markdown(f"""
        <div style="background-color: #262730; padding: 10px; border-radius: 5px; border-left: 5px solid {vix_color};">
            <div style="font-size: 0.8rem; color: #aaa;">MARKET VIX</div>
            <div style="font-size: 1.5rem; font-weight: bold; color: white;">{vix:.2f} <span style="font-size:0.8rem; color:{vix_color}">({vix_label})</span></div>
        </div>
    """, unsafe_allow_html=True)
    
    col2.metric("Scan Time", ts.split(" ")[1]) 
    col3.metric("Candidates", len(df))
    
    if col4.button("🔄 Refresh"): 
        load_radar_with_deltas.clear()
        st.rerun()
    
    auto_ref = col5.checkbox("Auto (5min)", value=st.session_state.auto_refresh)
    st.session_state.auto_refresh = auto_ref
    
    # --- 主表格 (Radar) ---
    display_df = df.copy()
    
    # 显式重命名列，找回希腊字母 Δ
    display_df = display_df.rename(columns={
        "d_10m": "Δ10m",
        "d_1h": "Δ1h"
    })
    
    # Emoji 上色
    def color_delta(val):
        color = '#00c853' if val > 0 else '#f44336' if val < 0 else '#888'
        return f'color: {color}'

    # [MODIFIED] 调整列顺序：将 'cal_score' 移到最后 (blueprint_json之前)
    cols = ['symbol', 'price', 'Δ10m', 'Δ1h', 'iv_short', 'edge', 'regime', 'strategy_type', 'tag', 'gate_status', 'cal_score', 'blueprint_json']
    cols = [c for c in cols if c in display_df.columns]
    display_df = display_df[cols]
    
    # 应用样式
    styled_df = display_df.style.format({
        "price": "${:.2f}",
        "Δ10m": "{:+.2f}",
        "Δ1h": "{:+.2f}",
        "edge": "{:.2f}",
        "iv_short": "{:.1f}%",
    }).map(color_delta, subset=['Δ10m', 'Δ1h'])

    column_cfg = {
        "blueprint_json": None, 
        "cal_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
        "symbol": st.column_config.TextColumn("Symbol", width="small"),
    }

    event = st.dataframe(
        styled_df, 
        width="stretch", 
        hide_index=True, 
        column_config=column_cfg, 
        selection_mode="single-row", 
        on_select="rerun", 
        height=550
    )
    
    # --- 侧边栏 ---
    if len(event.selection.rows) > 0:
        selected_index = event.selection.rows[0]
        # 注意：这里要用原始的 df 来获取数据，因为 display_df 列名变了
        row = df.iloc[selected_index]
        symbol = row['symbol']
        bp_json_raw = row['blueprint_json']
        
        with st.sidebar:
            st.markdown(f"## 🔭 Scope: **{symbol}**")
            
            gate_color = "#00c853" if row['gate_status'] == "EXEC" else ("#ffeb3b" if row['gate_status'] == "LIMIT" else "#f44336")
            strat_display = row['strategy_type'].replace("STRADDLE", "STRD").replace("DIAGONAL", "DIAG")
            
            st.markdown(f"""
            <div class="sidebar-header">
                <div class="header-item"><div class="header-label">STRATEGY</div><div class="header-value">{strat_display}</div></div>
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
                        if "DIAGONAL" in row['strategy_type'].upper() or "DIAGONAL" in str(row['tag']).upper():
                            target_strategy = "DIAGONAL"
                            for leg in legs:
                                cls = "leg-buy" if leg['action'] == "BUY" else "leg-sell"
                                icon = "🟢" if leg['action'] == "BUY" else "🔴"
                                st.markdown(f"""<div class="blueprint-box {cls}"><span>{icon} <b>{leg['action']} {leg['ratio']}x</b></span><span>{leg['exp']}</span><span><b>{leg['strike']} {leg['type']}</b></span></div>""", unsafe_allow_html=True)
                                if leg['action'] == 'SELL': short_exp = leg['exp']; short_strike = float(leg['strike'])
                                elif leg['action'] == 'BUY': long_exp = leg['exp']; long_strike = float(leg['strike'])
                        else:
                            target_strategy = "STRADDLE"
                            for leg in legs:
                                cls = "leg-buy" if leg['action'] == "BUY" else "leg-sell"
                                st.markdown(f"""<div class="blueprint-box {cls}"><span>{leg['action']} {leg['ratio']}x</span><span>{leg['exp']}</span><span><b>{leg['strike']} {leg['type']}</b></span></div>""", unsafe_allow_html=True)
                            if legs: short_exp = legs[0]['exp']; short_strike = float(legs[0]['strike'])
                except Exception as e: st.error(f"Blueprint Error: {e}")

            st.divider()
            urgency = st.radio("Pricing Mode", ["PASSIVE", "NEUTRAL", "AGGRESSIVE"], horizontal=True, label_visibility="collapsed")
            
            limit_price_display, est_cost_display, is_ready = "---", "---", False
            
            if bp_valid and short_exp:
                try:
                    sniper = get_sniper()
                    if sniper:
                        res = sniper.lock_target(symbol=symbol, strategy=target_strategy, short_exp=short_exp, short_strike=short_strike, long_exp=long_exp, long_strike=long_strike, urgency=urgency)
                        if res['status'] == 'READY':
                            is_ready = True
                            limit_price_display = f"${res['limit_price']:.2f}"
                            est_cost_display = f"${res['est_cost']:.0f}"
                        else: st.error(res.get('msg'))
                except Exception as e: st.error(f"Pricing Error: {e}")

            st.markdown(f"""<div class="calc-box"><div class="calc-title">CALCULATED LIMIT ({urgency})</div><div class="calc-price">{limit_price_display}</div><div class="calc-sub">Est. Cost: {est_cost_display}</div></div>""", unsafe_allow_html=True)
            
            c1, c2 = st.columns([4, 1])
            with c1:
                if st.button("🚀 SEND ORDER TO BROKER", type="primary", use_container_width=True, disabled=not is_ready):
                    st.toast(f"Order Sent! {symbol} @ {limit_price_display}", icon="✅")
            with c2:
                if st.button("🔄"): st.rerun()

else:
    st.warning("⚠️ No scan data found. Please run `python src/trade_guardian.py scanlist` first.")

# 倒计时循环
if auto_ref:
    countdown_box = st.empty()
    for i in range(300, 0, -5):
        countdown_box.caption(f"⏳ Auto-refresh in **{i}** seconds...")
        time.sleep(5)
    st.rerun()