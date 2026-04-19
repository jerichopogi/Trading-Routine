# Midday Review Routine

**When:** Weekdays 17:00 UTC
**Cron:** `0 17 * * 1-5`
**Purpose:** Cut losers, protect winners, mid-session risk check.

## Your job

1. Preflight + snapshot.
2. Review every open position.
3. Apply the rules:
   - **If a position is ≤ −0.5R**: close it.
   - **If a position is ≥ +1.0R and SL is not already at breakeven**: move SL to entry.
   - **If a position is ≥ +2.0R**: trail SL to lock in at least 1.0R of profit.

## Steps

```bash
python -m scripts.cli preflight --json
python -m scripts.cli snapshot --note "midday"
python -m scripts.cli positions --json
```

For losers past −0.5R:
```bash
python -c "
import os
from dotenv import load_dotenv; load_dotenv()
from scripts.broker import get_broker
from scripts import trade
b = get_broker(); b.connect()
# Close specific tickets identified from positions --json
for ticket in [<tickets>]:
    ok = b.close_position(ticket)
    print(ticket, ok)
b.disconnect()
"
```

For winners ≥ 1R, move to breakeven:
```bash
python -m scripts.cli breakeven --min-r 1.0
```

Journal what happened:
```bash
python -m scripts.cli journal --daily --section "midday review" --body "<what moved, why>"
```

Discord:
```bash
python -m scripts.cli notify --level info --title "Midday review" --body "<1-line summary>"
```

## Boundaries

- Do NOT open new positions in this routine (it's a management routine, not a research one).
- Do NOT remove stops, ever.
- Do NOT widen stops away from entry.
