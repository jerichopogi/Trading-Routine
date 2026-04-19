# Pre-Session Routine

**When:** Weekdays 06:45 UTC (London pre-open) AND 13:00 UTC (NY pre-open)
**Cron for scheduler:** `45 6 * * 1-5` and `0 13 * * 1-5`
**Instruments focus:** FX majors + XAUUSD/XAGUSD at 06:45 UTC; indices at 13:00 UTC

## Your job

1. Check preflight state. If hard stop is tripped, STOP — do not research, just notify and exit.
2. Research top 3 catalysts for this session's tradeable instruments.
3. Draft trade ideas (symbol, direction, rough entry/invalidation/target) into today's daily journal.
4. Do NOT place orders in this routine. The next routine (session-open) decides what executes.

## Steps

```bash
# 1. Preflight
python -m scripts.cli preflight --json
```

If `hard_stop_hit: true` or `firm_violation: true`:
  - Run `python -m scripts.cli notify --level error --title "Pre-session aborted" --body "<reasons from preflight>"`
  - Stop.

```bash
# 2. Snapshot equity for drawdown baseline
python -m scripts.cli snapshot --note "pre-session"
```

3. Read `memory/strategy.md` and `memory/playbook.md` — stay in character.

4. Pick 3–5 tradeable symbols from the preflight `tradeable_symbols`.
   Avoid duplicating any instrument already in `positions` (run `python -m scripts.cli positions`).

5. Research each one (or one combined query):
   ```bash
   python -m scripts.cli research --query "<your focused question for this session>" --json
   ```
   Keep the query specific — session bias, upcoming data, prior-day levels.

6. For each high-conviction idea, journal it:
   ```bash
   python -m scripts.cli journal --daily --section "<symbol> idea" --body "<plan>"
   ```
   Plan must include: direction, entry level, invalidation (SL), target (TP), 1-line thesis, catalyst timing.

7. Notify the summary:
   ```bash
   python -m scripts.cli notify --level info --title "Pre-session plan drafted" --body "<short bulleted ideas>"
   ```

## Boundaries

- Max 5 ideas journaled per run. More ideas = less discipline.
- If no high-conviction setup exists, journal "no trade" and move on. Patience is a feature.
- Do NOT edit `memory/strategy.md` or `memory/playbook.md` in this routine — those are weekly-review territory.
- Do NOT call `trade.place` or any order-placing helper.
