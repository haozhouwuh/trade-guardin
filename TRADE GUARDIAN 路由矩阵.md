**“Shape × Strategy × Gate” 路由矩阵**（Trade Graduation / Tactical Mode 专用）。
**先定形态(Shape)** → **再定策略(Route)** → **再定门槛(Gate)** → **最后给执行指令**。

---

# TRADE GUARDIAN 路由矩阵（Brain + Gate 统一版）

## 0) 核心输入（系统只看这几项）

* `em = edge_micro`（前端结构优势）
* `ek = edge_month`（后端结构优势）
* `short_dte`（短腿 DTE）
* `regime`（BACKWARDATION / CONTANGO / FLAT）
* `is_squeeze`（micro_iv > month_iv * 1.05）
* `curvature`（SPIKY_FRONT / NORMAL）
* `momentum`（CRUSH / QUIET / TREND / PULSE）
* `est_gamma`（风险硬阈值）
* Blueprint 是否成功（bp.error）

---

## 1) Shape 分类矩阵（先把地形说清楚）

按 **优先级** 从上到下匹配（命中即停止）：

| Priority | Shape        | 判定规则（建议标准）                                               | 交易含义               |
| -------: | ------------ | -------------------------------------------------------- | ------------------ |
|        1 | **BACKWARD** | `regime == BACKWARDATION`                                | 倒挂，卖近端=自杀          |
|        2 | **FFBS**     | `ek >= 0.20 and em < 0.08`                               | 前端平稳、后端陡峭：黄金对角线    |
|        3 | **SPIKE**    | `is_squeeze == True OR em >= 0.12`（*不要用 curvature 单独触发*） | 前端挤压/事件波：gamma 风险高 |
|        4 | **STEEP**    | `ek >= 0.20`                                             | 正常后端陡峭，有结构可吃       |
|        5 | **MILD**     | `0.15 <= ek < 0.20`                                      | 轻度结构，边缘一般          |
|        6 | **FLAT**     | `ek < 0.15`                                              | 结构平坦，靠纯波动          |

> 备注：你现在输出里 NVDA（ek=0.15）应该更像 **MILD** 而不是 STEEP。
> SPY 那种 “超短端低IV导致 curvature 虚触发” 不应直接变 SPIKE——SPIKE 必须靠 em / squeeze 确认。

---

## 2) Route（策略路由）矩阵：Shape → 选哪种策略

这部分就是你 Brain V5 的“结构优先”哲学 + 交易员现实：

| Shape        | 默认策略 Route                    | 原因（交易语言）                     |
| ------------ | ----------------------------- | ---------------------------- |
| **BACKWARD** | **LG（Long Gamma / Straddle）** | 倒挂时卖近端风险爆炸，只能买 gamma 防守      |
| **FFBS**     | **DIAG（Diagonal）**            | 前端低IV + 后端高溢价，Theta/Vega 差最肥 |
| **STEEP**    | **DIAG 优先**（若构建失败→LG）         | 有结构可收割，且前端未必危险               |
| **SPIKE**    | **LG 优先**（特殊情况才 DIAG）         | 前端挤压，短腿 gamma 反噬概率高          |
| **MILD**     | **LG 优先**（除非 ek 很稳/你想加条件）     | 结构边缘，做 DIAG 性价比一般            |
| **FLAT**     | **LG**                        | 没结构就赌波动扩张                    |

**补充：DIAG 构建失败兜底**

* `if diagonal.evaluate(ctx) 失败 or meta缺 long_strike` → 自动回落 **LG**
  （你已经做了）

---

## 3) Gate（放行矩阵）：Route + Shape + Momentum → WAIT / LIMIT / EXEC

Gate 是 “能不能做 / 怎么做” 的最后裁判，分三层：

### 3.1 Hard Kill（无条件 FORBID）

| 条件                        | Gate       |
| ------------------------- | ---------- |
| `bp is None` 或 `bp.error` | **FORBID** |
| `est_gamma >= 0.30`       | **FORBID** |
| `momentum == CRUSH`       | **FORBID** |

---

### 3.2 结构门槛（Soft Gate）

你现在的底线参数：

* `MICRO_MIN = 0.10`
* `MONTH_MIN = 0.15`

#### A) 对 LG（Long Gamma）

LG 要求“前端不能太烂”，否则买了也是磨损：

| 条件                                  | Gate     |
| ----------------------------------- | -------- |
| `em < MICRO_MIN AND ek < MONTH_MIN` | **WAIT** |
| 其它                                  | 进入动能判定   |

#### B) 对 DIAG（Diagonal）——核心：看 ek，微结构豁免 em

| Shape                   | DIAG 的结构要求                            | Gate        |
| ----------------------- | ------------------------------------- | ----------- |
| **FFBS / STEEP**        | `ek >= MONTH_MIN`（豁免 em）              | 进入动能判定      |
| **其它（FLAT/MILD/SPIKE）** | `ek >= MONTH_MIN AND em >= MICRO_MIN` | 否则 **WAIT** |

---

### 3.3 动能判定（EXEC vs LIMIT）

动能来自你的 15m IV 变化（QUIET/TREND/PULSE）：

| Momentum          | Gate      | 执行含义        |
| ----------------- | --------- | ----------- |
| **PULSE / TREND** | **EXEC**  | 动能确立：可以主动成交 |
| **QUIET**         | **LIMIT** | 等触发：挂单潜伏    |

---

## 4) 最重要的“交易员补丁”：SPIKE 的 DIAG 降级规则（强烈建议）

因为 SPIKE 是最容易把你炸掉的形态。

### 规则（推荐最小补丁）

* 如果 `tag` 是 DIAG 且 `shape == SPIKE` 且 `short_dte <= 7` 且 `momentum == QUIET`
  → **强制 WAIT**（不允许 LIMIT 被动吃 gamma 风险）

| 条件                                    | Gate               |
| ------------------------------------- | ------------------ |
| `DIAG & SPIKE & short_dte<=7 & QUIET` | **WAIT**           |
| `DIAG & SPIKE & (TREND/PULSE)`        | **EXEC**（只有动能确认才打） |

这条会让 TSLA/QQQ/TQQQ 这种 “结构好但前端不安稳” 不会天天 LIMIT。

---

# 最终总表（你可以贴 README 的“决策总矩阵”）

按实际执行顺序：

1. **Hard Kill**：bp/error, gamma, CRUSH → FORBID
2. **Shape**：BACKWARD / FFBS / SPIKE / STEEP / MILD / FLAT
3. **Route**：BACKWARD→LG；FFBS/STEEP→DIAG；SPIKE/MILD/FLAT→LG（SPIKE 特判）
4. **Gate-结构**：DIAG 看 ek（FFBS/STEEP 豁免 em）；LG 看 em/ek 双低
5. **Gate-动能**：TREND/PULSE→EXEC；QUIET→LIMIT
6. **SPIKE-DIAG 降级补丁**：SPIKE + DIAG + short_dte<=7 + QUIET → WAIT

---

## 快速 sanity check（用你当前输出验证）

* **IWM：FFBS + ek=0.24 + em=0.01**
  → Route=DIAG，Gate 不查 em（豁免）→ QUIET → LIMIT ✅
* **SPY：超短端低IV，但 em 低、ek=0.17（MILD）**
  → Route=LG → em/ek 是否双低？不双低 → QUIET → LIMIT（但你也可以让 MILD 默认 WAIT）
* **TSLA：SPIKE + DIAG + QUIET + short_dte=6**
  → 会被“SPIKE-DIAG 降级补丁”打回 WAIT（避免挂单被动吃风险）✅

---


# 🧠 Trade Guardian: Decision Engine Logic

Trade Guardian 采用 **"Shape-First" (形态优先)** 的决策逻辑，将期权期限结构数据转化为具体的战术指令。系统不再单纯依赖绝对波动率数值，而是依赖**相对结构优势**。

### 核心流程
`Raw Data` $\to$ `Stabilized Edges` $\to$ `Shape Classifier` $\to$ `Strategy Route` $\to$ `Safety Gate` $\to$ `Action`

---

## 1. 核心输入 (Key Inputs)

为了消除短端噪音并还原真实地形，系统在原始数据之上进行了数学稳定化处理：

* **Edge Micro (`em`)**: 前端结构优势。
    * *Stabilizer V3*: 采用 `1-10 DTE Median Base` 消除单点噪音，并对 `< 6 DTE` 进行连续平滑衰减。
* **Edge Month (`ek`)**: 后端结构优势。
    * *Anchor*: 动态锚定 `30-45 DTE` 战术区。
* **Regime**: 市场状态 (`BACKWARDATION` / `CONTANGO` / `FLAT`).
* **Momentum**: 15分钟级别 IV 动能 (`PULSE` / `TREND` / `QUIET` / `CRUSH`).

---

## 2. 形态分类矩阵 (Shape Matrix)

系统按 **优先级 (Priority)** 依次识别以下期限结构形态，命中即停止：

| 优先级 | 形态 (Shape) | 判定规则 (伪代码) | 交易含义 |
| :---: | :--- | :--- | :--- |
| **1** | **BACKWARD** | `Regime == BACKWARDATION` | **倒挂**。卖方禁区，防御模式。 |
| **2** | **FFBS** | `ek >= 0.20` & `em < 0.08` | **前平后陡** (Front-Flat Back-Steep)。黄金对角线形态。 |
| **3** | **SPIKE** | `Squeeze` 或 `em >= 0.12` | **前端刺头**。短期 IV 暴涨，Gamma 风险高。 |
| **4** | **STEEP** | `ek >= 0.20` | **陡峭**。标准的期限结构套利机会。 |
| **5** | **MILD** | `0.15 <= ek < 0.20` | **温和**。结构一般，处于临界点。 |
| **6** | **FLAT** | `ek < 0.15` | **平坦**。无结构优势，纯波动率博弈。 |

---

## 3. 策略路由 (Strategy Route)

基于 **Brain V5 (结构优先，低波兜底)** 哲学：

### 🟢 AUTO-DIAG (Diagonal Strategy)
**触发逻辑**：当结构优势明显时触发。
* **适用形态**：`FFBS`, `STEEP`, 或 `edge_month >= 0.20`。
* **核心思想**：只要坡度够陡，即使绝对 IV 较低，也优先利用时间价值衰减差异（Theta/Vega Arb）获利。

### 🔵 AUTO-LG (Long Gamma / Straddle)
**触发逻辑**：当结构平庸或波动率极低时触发。
* **适用形态**：`MILD`, `FLAT`, `BACKWARD`, `SPIKE` (通常), 或 `HV Rank < 30`。
* **核心思想**：没有结构优势时，买入跨式期权（Straddle）博取波动率回归或方向性突破。

---

## 4. 安全门阀 (Safety Gate V6)

Gate 是系统的最后一道防线，决定最终状态是 `EXEC` (执行)、`LIMIT` (挂单) 还是 `WAIT` (观望)。

### 🛑 Hard Kill (一票否决)
* **Blueprint Error**: 建仓失败或数据缺失 $\to$ `FORBID`
* **Gamma Risk**: `est_gamma >= 0.30` $\to$ `FORBID`
* **Vol Collapse**: `Momentum == CRUSH` $\to$ `FORBID`

### 🚧 Structural Gate (结构放行)

1.  **DIAG 豁免权 (The FFBS Privilege)**:
    * 若形态为 **`FFBS`** 或 **`STEEP`**，**豁免**对 Micro Edge (`em`) 的最低要求。
    * *理由：此时前端越平越好，不需要前端有 Edge。*

2.  **SPIKE 降级保护 (Rule #4)**:
    * 若形态为 **`SPIKE`** 且 `Short DTE <= 7` 且动能不足 (`QUIET`) $\to$ 强制 **`WAIT`**。
    * *理由：前端挤压时挂 Limit 单容易被动成交并立刻遭受 Gamma 反噬。*

3.  **LG 标准**:
    * 若 `em` 和 `ek` 双低 (`< Threshold`) $\to$ **`WAIT`**。

### 🚀 Momentum Gate (执行层)

* **`PULSE` / `TREND`**: 动能确立 $\to$ **`EXEC`** (建议 Market 或 Mid+)
* **`QUIET`**: 动能沉寂 $\to$ **`LIMIT`** (建议 Mid-)

---

## 5. 术语对照表

* **S_IV**: Short Leg IV (Base Denominator)
* **EdgM**: Micro Edge (Stabilized Front-end Slope)
* **EdgK**: Month Edge (Stabilized Back-end Slope)
* **Scr**: Score (综合评分)