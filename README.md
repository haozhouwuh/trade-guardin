

# 🧠 Trade Guardian (v2.0)

**Trade Guardian** 是一款面向专业期权交易者的自动化扫描与执行计划工具。它不仅能发现市场中定价偏低的波动率机会（Edge），更能通过内置的 **Planner (计划器)** 强制执行硬性风险拦截。

> **设计哲学**：Scanner 负责海选机会，Planner 负责确保可交易性与硬风险闸门。

---

## 🚀 核心进化功能

### 1. 真实数据驱动 (Schwab API)

系统已完全打通 **Schwab API**，实时获取：

* 标的现价、历史波动率 (HV) 及 HV Rank。
* ATM 期权链、隐含波动率 (IV) 期限结构及 Greeks (Gamma/Delta/Theta)。
* 自动锁定流动性最佳的 **月度期权合约 (Monthly OpEx)**。

### 2. 增强型风险引擎 (The Guardian)

* **Total Gamma 监测**：自动计算组合（如跨式组合 Straddle）的总 Gamma，并进行分级：
* **EXTREME ⛔** (Γ ≥ 0.20): 极端波动风险。
* **HIGH ⚠️** (Γ ≥ 0.12): 高风险，需大幅缩减仓位。
* **ELEVATED 🔸** (Γ ≥ 0.08): 预警区。


* **PMCC 安全锁**：硬性执行 `Debit < Width` 检查，自动拦截注定亏损的锁死交易（Locked Loss），并给出修正建议。

### 3. 交易员看板式输出

* **Gate 状态列**：在扫描结果中直观显示 `✅ 可执行`、`⚠️ 高风险`、`⛔ 已拒绝`。
* **Trader's Sort**：优先排列“高 Edge、低 Risk、Gate ✅”的标的。
* **Actionable Blueprints**：生成含具体日期、行权价、方向及成本估算的执行蓝图。

---

## 🛠️ 安装与配置

1. **环境要求**：Python 3.8+
2. **安装依赖**：
```bash
pip install requests pandas numpy

```


3. **数据配置**：
* 在 `data/tickers.csv` 中填入你想扫描的股票代码（每行一个）。
* 确保 `src/trade_guardian/infra/schwab_token_manager.py` 能够获取有效的 API Token。



---

## 📖 使用指南

### 启动全自动扫描

```bash
python src/trade_guardian.py scanlist --strategy auto --days 600 --detail --top 5

```

**参数说明**：

* `--strategy auto`: 同时运行 Long Gamma (Straddle) 和 Diagonal (PMCC) 策略。
* `--days 600`: 扫描远端期权链（用于 PMCC 寻找 LEAPS）。
* `--detail`: 输出具体的执行蓝图（Actionable Blueprints）。
* `--top 5`: 经过排序后，只显示最优质的前 5 个交易计划。

---

## 📊 输出解读

### 扫描表 (Scanner View)

| Sym | Px | ShortExp | DTE | ShortIV | Edge | Risk | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AMD | 202.0 | 2026-01-16 | 29 | 45.2% | +0.13x | 20 | ✅ |
| ONDS | 7.87 | 2026-01-16 | 29 | 120.4% | +0.26x | 70 | ⚠️ |
| MSTR | 159.2 | 2026-01-16 | 29 | 77.6% | +0.02x | 30 | ⛔ |

### 执行蓝图示例 (Execution Plan)

```text
 MSTR DIAGONAL    Est.Debit: $55.87
    +1 2026-06-18 111.0  CALL
    -1 2026-01-16 162.0  CALL
    ==============================
    ⛔ REJECTED: Debit > Width. Excess: $4.87.
    ==============================
    Strategy Gate: Blocked by Risk Policy.
       • Try buying deeper ITM LEAPS or RAISING Short Strike.

```

---

## 🧾 诊断指标 (Diagnostics)

* **Avg |Edge|**: 市场整体波动率偏离强度。
* **Cheap Vol (%)**: 市场中处于“便宜”状态（IV < HV）的标的比例，作为多空情绪温度计。

---

## ⚖️ 免责声明

本工具仅供研究与参考使用，不构成任何投资建议。期权交易涉及高风险，在使用蓝图执行交易前，请务必核实实时报价并进行个人风险评估。

---

**Current Version**: 2.0.0 | **Last Updated**: 2025-12-18