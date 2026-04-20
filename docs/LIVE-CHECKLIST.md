# Live Trading Checklist

Written after Phase 3 backtest (PR #10) proved that the LLM-applied rubric destroys 90% of the mechanical strategy's edge. This document is the action plan for preserving the mechanical **+386% / 4.4% max DD** backtest result in live trading.

Read this **in full** before enabling `ALLOW_LIVE_ORDERS=1` on a funded or challenge account.

## Contents

1. [Pre-launch — do these before the next session-open fires](#1-pre-launch)
2. [Revised session-open task prompts](#2-revised-session-open-task-prompts)
3. [Monitoring tolerances — pin these in Discord before trading](#3-monitoring-tolerances)
4. [What stays, what changes, what to avoid](#4-what-stays-what-changes-what-to-avoid)
5. [Week-by-week timeline](#5-week-by-week-timeline)
6. [Kill-switch procedures](#6-kill-switch-procedures)

---

## 1. Pre-launch

Complete every item before the next `session-open` scheduled task fires.

- [ ] Phase 3 PR (#10) merged to main
- [ ] `.env` configured per `docs/SETUP-WINDOWS.md`
- [ ] MT5 terminal running, logged in, Algo Trading green
- [ ] All 7 routines registered as Claude Desktop local tasks
- [ ] Windows Task Scheduler `trading-manage-runners` registered (15-min cadence)
- [ ] `run-manage.cmd` launcher in repo root, test-fires cleanly
- [ ] Session-open task prompts REPLACED with the mechanical block in [§2](#2-revised-session-open-task-prompts) — **critical**
- [ ] Pre-session task prompts KEPT — research + journaling is still valuable; only the grading-filter was harmful
- [ ] Discord webhook verified (send a test ping)
- [ ] First-run verification: `python -m scripts.cli preflight --json` returns valid JSON with your account details
- [ ] Monitoring thresholds in [§3](#3-monitoring-tolerances) pinned in Discord channel
- [ ] Kill-switch commands from [§6](#6-kill-switch-procedures) memorized

---

## 2. Revised session-open task prompts

**Replace** the Prompt field in both `session-open-london` and `session-open-ny` Desktop scheduled tasks with this exact block. Fill in `<SESSION>` per task (London or NY).

```
Execute the session-open routine: @.claude/routines/session-open.md

MECHANICAL EXECUTION MODE (post Phase-3 backtest decision).

For every journaled idea today, call trade.place() with the journaled
entry/stop/target — regardless of rubric items 2, 3, 5. The broker adapter
auto-converts to LIMIT or STOP pending if the entry is not at market. The
guardrails (scripts/guardrails.py) enforce risk, DD, concurrent cap, session
windows, and stop-too-tight checks. THAT is your safety layer.

SKIP an idea ONLY if one of these is true:
  1. The journaled thesis has been EXPLICITLY invalidated:
     - The specific catalyst the idea anchored on has already fired
       against the trade direction (e.g., NFP came out against the thesis)
     - The anchor level has been broken definitively on a closing basis
       (daily close through support for a long-at-support idea)
  2. An identical pending order already exists on MT5 for the same symbol
     + direction + entry (run `python -m scripts.cli pending --json` first
     to verify)
  3. Guardrails reject (max 5/day, max 3 concurrent, DD hit, risk > cap,
     symbol not whitelisted, session closed)

DO NOT skip because:
  - "Recent bars move counter to the trade direction" — this is EXPECTED
    for the Failed-Breakout Fade setup, which is counter-trend by design
  - "The rubric shows < 4/5" — Phase 3 backtest proved the rubric as
    written destroys the Fade setup's edge by rejecting 97.5% of its
    winners
  - "It's been > 60 minutes since the routine started" — pending orders
    don't chase; they let price come to us

Sizing:
  - Flat 0.5% risk per trade
  - DO NOT apply A-grade (1%) sizing — Phase 3 showed 5/5-rubric trades
    underperformed 4/5, so the A/B split is invalid until a new rubric
    is validated

After placements:
  python -m scripts.cli journal --daily --section "executions" --body \
    "<symbol>: <kind> placed, entry=<X> sl=<Y> tp=<Z>, ticket=<N>"

Final Discord notify body: separate counts for MARKET fills vs PENDING
placed, with the setup tag per trade.

Session focus for this run: <SESSION> open.
```

**Paste flow:**

1. Claude Desktop → Schedule → click `session-open-london` → pencil icon
2. Paste the block above into Prompt, replace `<SESSION>` with `LONDON`
3. Save
4. Repeat for `session-open-ny`, replacing `<SESSION>` with `NY`
5. Click **Run now** on one of them once (no impact if no ideas journaled yet) to pre-approve Bash/Edit permissions

---

## 2b. Revised session-close task prompt — daily pending-order cleanup

The routine `.claude/routines/session-close.md` flattens positions (plus pending orders, via `trade.flatten_all(cancel_pending=True)`) **only on Fridays**. Mon-Thu it closes "intraday"-tagged positions but leaves every un-triggered pending order sitting on MT5 **overnight**. Those pending orders carry a thesis baked in at placement time — by next morning's Asian session, conditions may have shifted and the stale level may fill in a regime that no longer supports the original idea.

Fix: override the session-close task prompt to cancel every pending order daily, not just Friday.

**Replace** the Prompt field on the `session-close-ny` Desktop scheduled task with this:

```
Execute the session-close routine: @.claude/routines/session-close.md

END-OF-DAY PENDING ORDER CLEANUP (post-Phase-3 tightening).

Regardless of whether today is Friday or not, after the routine's normal
flow (journaling, intraday close, Friday-flatten branch), ALWAYS cancel
every un-triggered pending order before ending the session. Stale pending
orders from this session's thesis must not carry into tomorrow's Asian
opening when conditions may have shifted.

Run this after the routine's journaling step:

    python -m scripts.cli cancel-pending --reason "session-close daily sweep"

Then verify:

    python -m scripts.cli pending --json

Expected output: `[]` (empty). If any pending remains, investigate and
retry cancellation — do NOT end the session with pending orders live.

Final Discord notify body must include a line:
    "Pending orders cancelled: N"

(The Friday-flatten branch already cancels pending via flatten_all with
cancel_pending=True; the daily sweep above is additional insurance and
covers Mon-Thu explicitly.)
```

**Why this is safe:**

- `cancel-pending` only touches un-triggered pending orders (never positions, never fills)
- Re-drafting next session is cheap — pre-session builds fresh context with today's catalysts
- Friday behavior unchanged (flatten_all still runs first); now Mon-Thu matches

**Optional belt-and-braces:** add the same `cancel-pending` call to `midday-breakeven` to kill any pending sitting > 8 hours. Session-close alone is sufficient; this is only if you want tighter overnight hygiene.

**Paste flow:**

1. Claude Desktop → Schedule → `session-close-ny` → pencil icon
2. Replace the Prompt field with the block above
3. Save
4. Optional: click **Run now** once to pre-approve permissions

---

## 3. Monitoring tolerances

Copy-paste these into a pinned Discord message before trading starts. Check daily.

### Per-metric thresholds

| Metric | Backtest (1-year) | Live tolerance band | Breach action |
|---|---|---|---|
| Win rate (all trades, rolling 50) | 40.7% | 30% – 50% | < 30% after 50 trades → pause, investigate |
| Avg R per trade (rolling 50) | +0.520 | ≥ +0.20 | < +0.20 after 50 trades → halve risk_pct in config, re-validate |
| Max overall drawdown | 4.4% | ≤ 7% | Hits 7% → stop trading; review 4 weeks of trades before resuming |
| Per-setup WR (Fade, ≥ 20 trades) | ~47% | ≥ 35% | < 35% → structural difference vs backtest; likely spread/slippage issue |
| Per-setup WR (Gold Pullback, ≥ 20 trades) | ~33% | ≥ 25% | < 25% → check if manage-runners is firing correctly |
| Consecutive losers | 5 (rare) | ≤ 7 | Hits 8 → pause next day, check journal for pattern |
| Average spread paid (vs 0.2 pip backtest) | 0.2 pips | ≤ 1.5 pips | > 1.5 pips consistently → ECN account or broker change |

### Daily check (5 min, every trading day)

- [ ] Review Discord: trade placements, management events, rejections
- [ ] Eyeball MT5 Trade tab: any positions I didn't expect?
- [ ] Check `memory/daily-journal/<today>.md`: reasoning makes sense?
- [ ] Check `logs/manage-runners-*.log` for errors
- [ ] Update the rolling 50-trade tracker (a simple spreadsheet: symbol / setup / R / cumulative)

### Weekly check (30 min, Saturday after weekly-review runs)

- [ ] Read the auto-generated `memory/weekly-reviews/<iso-week>.md`
- [ ] Compare this week's stats against the tolerance table above
- [ ] If any metric is at the edge of the tolerance, document the reason
- [ ] If strategy.md / playbook.md proposals from the review are sensible, apply them manually

---

## 4. What stays, what changes, what to avoid

### KEEP (unchanged from backtest)

| Component | Why |
|---|---|
| `scripts/guardrails.py` | Risk enforcement. Non-negotiable. |
| `scripts/management.py` (manage-runners) | Phase 2a proved +1.5R/year impact on LBO alone; critical for Fade and Gold |
| Pending orders (LIMIT/STOP auto-convert) | Catches signals between routines; validated in Phase 2b |
| `scripts/broker/*` (MT5 adapter + Mock) | Attach-first logic, auto-filling, all verified |
| Config `config/fundednext.yml` | 0.5% / 1% caps, DD limits, whitelist |
| Pre-session research routines | Discord summaries are useful context; no trades placed here |
| Midday + session-close + weekly-review routines | Mechanical or audit-only; no rubric filter to remove |

### CHANGE (post Phase 3)

| Thing | From | To |
|---|---|---|
| Session-open task prompts | CRITICAL OVERRIDE rubric-grading block | MECHANICAL EXECUTION block in [§2](#2-revised-session-open-task-prompts) |
| Per-trade risk | "A=1%, B=0.5%" | Flat 0.5% until valid A/B rubric exists |
| Trade skip criteria | Rubric score < 4/5 | Only explicit thesis invalidation / duplicate / guardrail reject |

### AVOID (do not do these)

- Applying the original rubric to live Fade trades → **destroys 90% of edge**
- Raising risk to 1% on any "A-grade" → **A-grade cohort underperformed B in backtest**
- Adding new setups to the playbook mid-live → unvalidated; test in backtest first
- Disabling manage-runners "to simplify" → flips LBO from +0.69 to −0.83 total R
- Removing guardrails to "let the strategy breathe" → they are the prop-firm compliance layer
- Starting on funded account without demo first → minimum 2-4 weeks on demo/challenge
- Overriding stop-too-tight guardrail → stops get wicked out systematically
- Extending `pending_expire_hours` beyond 8 → stale intents carry into regime changes
- Disabling session-close `flatten_all(cancel_pending=True)` → pending orders leak overnight

---

## 5. Week-by-week timeline

Realistic timeline assuming the Phase 3 remediation works and live tracks backtest within tolerance.

| Week | Activity | Target |
|---|---|---|
| **W-1 (this week)** | Phase 3 PR merged, session-open prompts updated, Task Scheduler configured, demo account smoke-tested | All pre-launch checklist items ✓ |
| **W1** | Demo trading begins. Claude Desktop routines fire on schedule. Manage-runners every 15 min. | First full week of data; compare per-metric vs tolerance |
| **W2** | Continue demo. Weekly review generates first real report. | Minimum 20-30 trades to start evaluating |
| **W3** | If tolerances hold, continue demo. If breached, pause and investigate. | ≥ 50 trades; statistically-meaningful comparison |
| **W4** | Demo decision point: continue demo for another 2 weeks or switch to challenge? | Live tracking ≥ 70% of backtest = greenlight challenge |
| **W5-8** | FundedNext Phase 1 challenge (8% target, 5% daily DD, 10% overall). Backtest says mechanical hits at trade #33 / day 22. Realistic with haircuts: 4-8 weeks. | Pass Phase 1 |
| **W9+** | FundedNext Phase 2 (5% target). Same rules, lower bar. Backtest says 2.5 weeks mechanical. Realistic: 3-5 weeks. | Pass Phase 2; get funded |

Total realistic time to funded: **8-12 weeks** if everything tracks. Can be longer without consequence — FundedNext has no time limit on the challenges.

---

## 6. Kill-switch procedures

Memorize these. If anything feels wrong, stop the machine first, diagnose second.

### Immediate halt — from any Windows terminal

```powershell
cd "C:\Users\jeric\OneDrive\Documents\AI Shits\Trading Routine\Trading-Routine"

# 1. Close every open position + cancel every pending order
python -m scripts.cli flatten --reason "manual kill"

# 2. Disable the manage-runners Task Scheduler job
Unregister-ScheduledTask -TaskName trading-manage-runners -Confirm:$false

# 3. Disable Algo Trading in MT5 (stops any further automated orders)
#    Open MT5 -> Ctrl+E -> button turns red
```

### Pause without halting

```powershell
# Disable manage-runners but keep positions/pending alive
Disable-ScheduledTask -TaskName trading-manage-runners

# In Claude Desktop: go to each scheduled routine and TOGGLE OFF
# (the task stays configured; next fire is skipped)
```

### Emergency: revert to pre-Phase-3 prompt

If the new mechanical-execution prompt produces unexpected behavior (placing too many trades, for example), re-enable the Desktop task's previous prompt but temporarily set `risk.max_new_positions_per_day: 1` in `config/fundednext.yml` until root cause is found.

### Find the log trail after an incident

```powershell
# manage-runners job logs
type logs\manage-runners-$(Get-Date -Format yyyy-MM-dd).log

# Claude routine sessions (look in Claude Desktop's session list,
# filtered to "Scheduled")

# Trade log (structured JSONL of every order event)
type memory\trade-log.jsonl | Select-String -Pattern "rejection" | Select-Object -Last 20
```

---

## One final sanity note

The Phase 3 finding is important but narrow: **the specific rubric as written, applied uniformly across trend and counter-trend setups, is anti-edge.** It does not mean LLM judgment is useless for trading — it means a *context-free rubric score* is useless, and probably worse than nothing.

LLM is still valuable for:
- Pre-session research (catalyst summarization, macro bias)
- Weekly review (qualitative grading, proposing playbook edits)
- Risk veto on specific conditions (red-folder within 30 min, catastrophic HTF break against direction)
- Anomaly detection in the trade log ("that was a weird fill")

It is NOT valuable for:
- Yes/no gating of individual mechanical setups with a counter-trend component
- A/B sizing based on a 5-point rubric that includes trend alignment

Keep the LLM where it adds value. Remove it where it costs edge.
