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

