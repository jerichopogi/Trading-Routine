# Playbook

_Last updated: 2026-04-20_

Only setups in this file are tradeable. If it's not here, it doesn't exist.
The weekly-review routine may propose additions/removals; humans apply them.

---

## 1. London Open Breakout (FX)

**When:** 07:00–09:00 UTC, FX majors (EURUSD, GBPUSD, AUDUSD, USDCAD).
**Trigger:** Break and close of the Asian-session range (00:00–07:00 UTC) on M15.
**Entry:** Retest of the broken range edge.
**Invalidation:** Midpoint of the Asian range.
**Target:** 1.5× the Asian range width from the break point, OR prior-day high/low.
**Filter:** Asian range ≥ 20 pips; session opens with expanding spread = skip.

---

## 2. NY Open Momentum (Indices)

**When:** 13:30–14:30 UTC, US30 + NAS100 + SPX500.
**Trigger:** First 15-minute cash-session bar closes above/below pre-market range.
**Entry:** Market on break of first-15m high/low in the direction of the close.
**Invalidation:** Opposite side of the first-15m bar.
**Target:** 2× the first-15m range.
**Filter:** No trade if there is a scheduled US data release within the next 10 minutes.

---

## 3. Gold Trend Pullback (XAUUSD)

**When:** H1 trend in place (EMA20 > EMA50 for long, opposite for short), London or NY session.
**Trigger:** Pullback to EMA20 with rejection wick on H1.
**Entry:** Break of the rejection candle's high (long) / low (short).
**Invalidation:** Below EMA50 (long) / above EMA50 (short).
**Target:** 2× risk OR prior H4 swing, whichever comes first.
**Filter:** Avoid NFP week Thursday/Friday. Avoid FOMC days.

---

## 4. FX Failed-Breakout Fade (Majors)

**When:** FX major breaks H4 swing high/low but closes back inside within 2 hours.
**Trigger:** Reclaim of the prior level + follow-through candle.
**Entry:** Market on the follow-through candle close.
**Invalidation:** Beyond the failed-breakout extreme + 5 pips.
**Target:** Opposite side of the H4 range.
**Filter:** No fade in the first hour of London or NY open (too early to confirm failure).

---

## 5. Range Scalp — DISABLED

_Disabled on prop accounts: fills and spread widening make this unreliable. Do not trade._

---

## Trade sizing — conviction based

Every trade is graded A or B. The grade drives risk %, and the LLM records
the rubric in the daily journal for later analysis.

### Grading rubric (from strategy.md)

Score 1 point per item true at intended entry:

1. **Matches a specific playbook setup** ← required; if 0, do not trade
2. **HTF trend aligned** with direction (H4 for intraday, D1 for swing)
3. **Clear of red-folder news** for next 60 min
4. **R:R ≥ 2.0** at planned entry/stop/target
5. **At a meaningful level** (prior HH/LL, key MA, untouched supply/demand)

- **5/5 → A-grade: 1.0% risk** (0.8% on XAU/NAS)
- **≤ 4/5 → B-grade: 0.5% risk** (0.4% on XAU/NAS)
- **Fails #1 → skip entirely**

### How to size in code

```python
from scripts.decide import draft_order, ConvictionGrade, SetupRubric

rubric = SetupRubric(
    matches_playbook=True,
    htf_trend_aligned=True,
    clear_of_news=True,
    rr_ratio_ok=True,
    at_meaningful_level=True,
)
grade = rubric.grade   # A if 5/5, else B

order = draft_order(
    symbol="EURUSD", side=OrderSide.BUY,
    entry=1.0750, stop=1.0720, target=1.0810,
    broker=b, grade=grade, comment="London breakout",
)
# Guardrail still enforces 1.0% ceiling (0.8% on XAU/NAS) — unbreakable.
```

Never override the computed lot size. The computation is the rule.

## Comment tagging

Use comment field so session-close routine can pick intraday positions:

- `intraday-<setup>-<symbol>` for positions that must close by NY session close
- `swing-<setup>-<symbol>` for positions held overnight (Mon–Thu only)
- All positions close on Friday regardless of tag.
