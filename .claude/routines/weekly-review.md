# Weekly Review Routine

**When:** Friday 22:00 UTC
**Cron:** `0 22 * * 5`
**Purpose:** Grade the week, propose strategy/playbook updates, ship a report to Discord.

## Your job

1. Read the week's `memory/daily-journal/*.md`.
2. Read `memory/trade-log.jsonl` for this week.
3. Read `memory/equity-curve.jsonl` for this week.
4. Compute: number of trades, win rate, average R, biggest winner, biggest loser, rule rejections.
5. Grade the week A–F against the playbook.
6. Propose (do NOT silently apply) edits to `memory/strategy.md` and `memory/playbook.md`.
7. Write the review file and Discord-ping the full report.

## Steps

```bash
python -m scripts.cli snapshot --note "weekly close"
python -m scripts.cli positions --json      # verify no open positions (Friday flatten already ran)
```

Read trade log for the last 7 days (filter by ts). Compute:
- trades_placed, trades_rejected
- winners, losers
- avg R, total R
- worst drawdown intra-week
- adherence to playbook (did we take only setups from playbook.md?)

Write the review:
```bash
python -m scripts.cli journal --section "week summary" --body "<filled-in report>"
```

Grade:
- **A**: rules clean, positive R, playbook-aligned
- **B**: rules clean, flat-to-small-positive R
- **C**: 1 minor rule bend, small loss
- **D**: rule violation OR −3R+ week
- **F**: hard-stop hit OR multiple rule violations

Propose edits:
- Write any suggested `strategy.md` or `playbook.md` changes to a `section: "proposed edits"`
  block in the weekly review file. Do NOT edit those files in this routine — humans apply.

Discord:
```bash
python -m scripts.cli notify --level info --title "Weekly review — grade <X>" --body "<summary>"
```

## Boundaries

- Do NOT modify `memory/strategy.md` or `memory/playbook.md` automatically. Propose only.
- Do NOT modify `config/fundednext.yml` ever.
- Do NOT place orders.
