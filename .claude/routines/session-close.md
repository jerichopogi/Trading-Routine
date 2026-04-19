# Session-Close Routine

**When:** Weekdays 21:00 UTC (NY close)
**Cron:** `0 21 * * 1-5`
**Purpose:** End-of-day journaling. Flatten intraday. On Fridays, flatten EVERYTHING.

## Your job

1. Preflight + snapshot.
2. If Friday — flatten all positions and skip to journaling.
3. Otherwise, close positions marked "intraday" in their comment. Leave swing positions.
4. Write the day's journal summary.

## Steps

```bash
python -m scripts.cli preflight --json
python -m scripts.cli snapshot --note "session-close"
python -m scripts.cli positions --json
```

### Friday branch
```bash
# Check if today is Friday (UTC)
python -c "from datetime import datetime, UTC; import sys; sys.exit(0 if datetime.now(UTC).weekday()==4 else 1)" && \
  python -m scripts.cli flatten --reason "Friday weekend flatten (no weekend hold)"
```

### Normal branch (Mon–Thu)
Only close positions where `comment` contains "intraday":
```bash
python -c "
import os
from dotenv import load_dotenv; load_dotenv()
from scripts.broker import get_broker
from scripts import journal
b = get_broker(); b.connect()
for p in b.positions():
    if 'intraday' in (p.comment or '').lower():
        ok = b.close_position(p.ticket)
        journal.log_close(position=p, ok=ok, reason='session-close intraday', stage=os.environ.get('TRADING_STAGE','dev'))
        print(p.ticket, ok)
b.disconnect()
"
```

### Journal the day

Summarize in `memory/daily-journal/<today>.md`:
- Number of trades placed vs rejected (count from trade-log.jsonl)
- Day P/L: equity change vs first snapshot of the day
- What the plan said vs what actually happened
- 1 lesson for tomorrow (this is the valuable bit)

```bash
python -m scripts.cli journal --daily --section "close-of-day summary" --body "<structured summary>"
python -m scripts.cli notify --level success --title "Day closed" --body "<P/L and 1-line takeaway>"
```

## Boundaries

- Do NOT open new positions.
- On Fridays, every single position MUST be closed. Verify with `positions` after flatten.
