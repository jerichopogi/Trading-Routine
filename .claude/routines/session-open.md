# Session-Open Routine

**When:** Weekdays 07:05 UTC (London open) AND 13:35 UTC (NY open, 5m after cash open)
**Cron:** `5 7 * * 1-5` and `35 13 * * 1-5`
**Purpose:** Execute the ideas that pre-session drafted, with discipline.

## Your job

1. Preflight + snapshot.
2. Read today's daily journal (`memory/daily-journal/YYYY-MM-DD.md`) for drafted ideas.
3. For each idea whose entry condition is live, place the order through the guarded trade path.
4. Record outcomes in the journal and ping Discord.

## Steps

```bash
# 1. Preflight. Abort on hard stop.
python -m scripts.cli preflight --json
```
If any violation → `notify --level error` and stop.

```bash
# 2. Snapshot
python -m scripts.cli snapshot --note "session-open"
```

3. Read drafted ideas from `memory/daily-journal/$(date -u +%F).md`.

4. For each idea:
   - Check current bid/ask via `python -m scripts.cli positions --json` has no conflicting position
   - Confirm the entry condition from the journal is still valid (price, catalyst timing)
   - **Query similar past trades** — this is the feedback loop:
     ```bash
     python -m scripts.cli similar --symbol EURUSD --setup london-breakout --limit 10
     ```
     If the last 10 instances of this setup-symbol pair have a losing cohort
     (Avg R negative AND win rate < 40%), demote the grade by one tier OR skip.
     Journal this reasoning explicitly.
   - **Grade the setup with the rubric** (see `memory/playbook.md` — 5 checks):
     1. Matches a playbook setup? (required)
     2. HTF trend aligned?
     3. Clear of red-folder news (next 60 min)?
     4. R:R ≥ 2.0?
     5. At a meaningful level?
     → 5/5 = A-grade (up to 1%). Anything less = B-grade (0.5%).
     Fails #1 = skip.
   - Execute with a short Python one-liner that goes through the guarded trade path:
     ```bash
     python -c "
     import os
     from dotenv import load_dotenv; load_dotenv()
     from scripts.broker import get_broker, OrderSide
     from scripts.decide import ConvictionGrade, SetupRubric, draft_order
     from scripts import trade
     b = get_broker(); b.connect()
     rubric = SetupRubric(
         matches_playbook=True,
         htf_trend_aligned=True,
         clear_of_news=True,
         rr_ratio_ok=True,
         at_meaningful_level=False,   # ← fill in honestly for each trade
     )
     order = draft_order(
         symbol='EURUSD', side=OrderSide.BUY,
         entry=1.0750, stop=1.0720, target=1.0810,
         broker=b, grade=rubric.grade, comment='London breakout',
     )
     outcome = trade.place(order, b, stage=os.environ['TRADING_STAGE'])
     print(outcome)
     b.disconnect()
     "
     ```
   - Journal the rubric for EVERY trade placed so weekly review can analyze A vs B performance.
   - The `trade.place` call runs the full guardrail battery; any violation returns a
     rejection with reasons. Do NOT bypass it.

5. Journal executions:
   ```bash
   python -m scripts.cli journal --daily --section "executions" --body "<what placed and why>"
   ```

6. Notify summary:
   ```bash
   python -m scripts.cli notify --level success --title "Session open: N trades placed" --body "<list>"
   ```

## Boundaries

- Max `risk.max_new_positions_per_day` per day — enforced by guardrails.
- Per-trade risk ≤ 0.5% — enforced by guardrails.
- If an idea's invalidation is within spread + 2 × point, skip it (set too tight).
- If it's been > 60 minutes since the routine started, do NOT chase — journal a skip.
