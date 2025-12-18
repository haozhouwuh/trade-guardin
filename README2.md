# Trade Guardian

**Trade Guardian** is a volatility-aware calendar-spread scanner designed for discretionary options traders.

It focuses on **structure quality, volatility regime, and risk explainability** rather than automation or backtesting.

This project is intentionally opinionated and transparent:
every score, filter, and rejection is explainable.

---

## What Problem Does Trade Guardian Solve?

Most option scanners answer:
> “What looks expensive?”

Trade Guardian answers:
> **“Is this calendar structure tradable *now*, given volatility, term structure, and risk?”**

---

## Core Concepts

### 1. Calendar Structure First
Trade Guardian evaluates **calendar spreads** by comparing:
- Short-dated ATM implied volatility
- Longer-dated baseline implied volatility
- Term structure regime (Contango / Backwardation / Flat)
- Front-end curvature (spiky or smooth)

No direction, no delta betting — structure first.

---

### 2. HV-Aware Filtering (#2 – Completed)

Implied volatility alone is misleading.

Trade Guardian integrates **realized volatility context**:
- 20-day HV
- HV Rank (rolling 1Y window)
- Percentile thresholds (P50 / P75 / P90)

This allows the scanner to distinguish:
- “IV looks high” vs
- “IV is high *relative to realized behavior*”

HV affects:
- Score bonuses/penalties
- Strategy suitability
- Risk interpretation

---

### 3. Explicit Risk Modeling

Each candidate includes:
- A **continuous risk score** (0–100)
- Fully decomposed risk breakdown:
  - Base exposure
  - Time-to-expiry
  - Gamma proxy
  - Regime risk
  - Curvature risk
- Strict enforcement rules:


# cal_score >= MIN_SCORE
# short_risk <= MAX_RISK


		No hidden filters.

		---

		## Output Categories

		### ✅ Strict Candidates
		Actionable under current rules:
		- Score meets threshold
		- Risk is within tolerance

		### 🤖 Auto-Adjusted
		Original structure fails risk,
		but an **alternative short expiry within probe range** is recommended.

		### 👀 Watchlist
		Score acceptable, but risk remains elevated.
		Useful for monitoring IV repricing.

		---

		## CLI Usage

		```bash
      python -m trade_guardian.app.cli scanlist \
      --strategy hv_calendar \
      --days 600 \
      --detail


      Key Flags

      --strategy hv_calendar
      HV-aware calendar evaluation

      --days
      Forward term horizon

      --detail
      Show per-row score and risk explanations
    ```

# Explainability Example：

	score=72 [b+50 rg+4 ed+8 hv+10 cv+0]
	risk=68  [b+35 dte+14 gm+20 cv+0 rg+4]

# Legend:

## b = baseline contribution

## rg = term-structure regime

## ed = IV edge (short vs baseline)

## hv = realized volatility context

## cv = front-end curvature

Every number has a reason.



# What Trade Guardian Is NOT

	❌ Not an auto-trading bot

	❌ Not a backtesting engine

	❌ Not a signal generator

	❌ Not optimized for execution speed

It is a decision-support tool.

# Roadmap
✅ #1 Calendar Scanner

Completed

✅ #2 HV-Aware Strategy

Completed

🔜 #3 Trade Blueprint (Planned)

# Convert scored candidates into explicit trade blueprints, including:

	Short leg

	Long leg

	Estimated debit

	Structural risk notes

Blueprints will remain advisory — no automation.

# Philosophy

	“If you can’t explain why a trade exists,
	you shouldn’t be in it.”

Trade Guardian exists to enforce that discipline.