
# Trade Guardian 🧠📊  
**Explainable Calendar Spread Scanner & Risk Engine**

Trade Guardian 是一个专注于 **期权日历价差（Calendar Spread）** 的扫描与评估引擎，核心目标不是“给信号”，而是：

> **把「为什么这个结构值得/不值得做」解释清楚**

它强调：
- 📐 **期限结构（Term Structure）**
- ⚖️ **IV Edge（短腿 vs 基准）**
- 🔍 **可解释的评分与风险拆解**
- 🚫 **明确告诉你“现在不该交易”**

---

## ✨ 核心特性

### 1️⃣ Explainable Scoring（可解释评分）
每个候选结构都会生成完整的评分拆解：

```

score=34 [b+50 rg-8 ed-14 hv+0 cv+6]

```

含义清晰、可追溯：

| 缩写 | 含义 |
|----|----|
| b  | base score（策略基础分） |
| rg | regime（期限结构形态：Contango / Backwardation / Flat） |
| ed | edge（短腿 IV 是否“贵”于基准） |
| hv | HV rank 占位（预留给 #2/#3） |
| cv | curvature（前端是否尖刺） |
| pen | 额外惩罚（如未来策略扩展） |

---

### 2️⃣ Continuous Risk Model（连续风险模型）
风险不再是“3 天=高风险 / 10 天=低风险”的硬切分，而是连续、可解释的：

```

risk=68 [b+35 dte+13 gm+17 cv+1 rg+2]

```

| 风险项 | 含义 |
|----|----|
| b   | 基础风险 |
| dte | 到期时间风险（越近越危险） |
| gm  | Gamma 代理（前端敏感度） |
| cv  | 曲率风险（仅在 squeeze 达标时触发） |
| rg  | Regime 风险（如 Contango） |

---

### 3️⃣ 明确的“不要交易”信号
Trade Guardian **不会强行给你机会**：

- 如果 **Edge 不够**
- 如果 **风险整体过高**
- 如果 **结构只是“看起来很活跃”**

系统会明确告诉你：

```

▶ Bottleneck: edge is weak (short not rich vs baseline).
Consider waiting for IV repricing.

````

---

## 🖥️ 当前支持策略

### #1 Calendar Spread（已完成）
- 基于短腿 rank + 30–90D 基准
- 支持 probe ranks（base → base+N）
- 同时输出：
  - Strict candidates
  - Auto-adjusted candidates
  - Watchlist
  - Top Overall（解释优先）

### #2 / #3（规划中）
- HV Rank / Vol Regime 强化
- Long Gamma / Event-aware Calendar
- Dynamic baseline selection

---

## 🚀 快速开始

### 环境要求
- Python ≥ 3.9
- Windows / macOS / Linux

### 安装依赖
（假设你已有虚拟环境）
```bash
pip install -r requirements.txt
````

### 运行扫描

```bash
python -m trade_guardian.app.cli scanlist --days 600 --detail
```

---

## 📤 示例输出（节选）

```
🏆 Top Overall (ranked by score + edge + lower risk)
SPY   score=52  risk=79  tag=BS
COIN  score=42  risk=68  tag=FS
AMD   score=34  risk=68  tag=CS
```

```
Top details (per-row explain)
SPY score=52 [b+50 rg+4 ed-8 hv+0 cv+6]
     risk=79 [b+35 dte+17 gm+20 cv+3 rg+4]
```

---

## 🧠 设计理念

Trade Guardian 并不是一个“自动交易系统”，而是一个：

> **结构级别的过滤器 + 认知放大器**

它回答的不是：

* “要不要买？”

而是：

* “这个日历价差 **为什么** 在当前市场环境下不具备优势？”

---

## 📂 项目结构（简化）

```
trade_guardian/
├── src/trade_guardian/
│   ├── app/          # CLI / Renderer / Orchestrator
│   ├── domain/       # Models / Scoring / Policy
│   ├── strategies/  # Calendar (#1), future #2/#3
│   └── data/
├── cache/            # 本地缓存（已忽略）
├── .gitignore
└── README.md
```

---

## ⚠️ 风险声明

本项目仅用于 **研究与辅助决策**，不构成投资建议。
期权交易具有高度风险，请自行评估。

---

## 🧭 Roadmap（真实，不画饼）

* [x] Calendar Scanner（Explainable）
* [x] Continuous Risk Model
* [x] Renderer Diagnostics
* [ ] Strategy #2（HV / Regime driven）
* [ ] Strategy #3（Event / Gamma aware）
* [ ] Backtest hooks（非强依赖）

---

## 👤 作者

**Hao Zhou**
Quant / Options Structure Research

---




