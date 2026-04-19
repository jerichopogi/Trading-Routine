# Trading Routine — MT5 + FundedNext AI Agent

A Claude Code-driven trading agent that runs on schedule, researches, places
and manages trades against a MetaTrader 5 broker, and respects FundedNext
Stellar 2-step prop firm rules as hard-coded guardrails.

Inspired by the "24/7 AI trading agent" workflow on Alpaca, re-targeted to
MT5 + FundedNext with session-aware scheduling and prop-firm kill-switches.

---

## What it does

### Routine schedule

Storage + cron times are in UTC. Display timezone is configurable (default
`America/New_York`). Times shown below also include NY and Manila equivalents.

| Routine        | UTC           | New York (ET)   | Manila (PHT)    | Job |
|----------------|---------------|-----------------|-----------------|-----|
| Pre-session 1  | Mon–Fri 06:45 | 02:45 EDT       | 14:45 PHT       | Research FX/gold — London pre-open |
| Session-open 1 | Mon–Fri 07:05 | 03:05 EDT       | 15:05 PHT       | Execute London-session ideas |
| Pre-session 2  | Mon–Fri 13:00 | 09:00 EDT       | 21:00 PHT       | Research indices — NY pre-open |
| Session-open 2 | Mon–Fri 13:35 | 09:35 EDT       | 21:35 PHT       | Execute NY-session ideas |
| Midday         | Mon–Fri 17:00 | 13:00 EDT       | 01:00 PHT (+1)  | Cut losers, trail winners |
| Session-close  | Mon–Fri 21:00 | 17:00 EDT       | 05:00 PHT (+1)  | Flatten intraday (Fri: flatten all), journal |
| Weekly review  | Fri 22:00     | Fri 18:00 EDT   | Sat 06:00 PHT   | A–F grading, playbook proposals |

_NY times use EDT (summer). Winter is EST — everything shifts 1 hour earlier in NY/ET._
_Manila (PHT, UTC+8) does not observe DST._

Set `DISPLAY_TIMEZONE` in `.env` to control what you see in Discord pings and
daily journal filenames. Internal timestamps stay UTC for DST safety.

All order placement passes through `scripts/guardrails.py`, which enforces
every FundedNext rule in code — the LLM cannot bypass it.

## Architecture

```
Claude Code routine (prompt)
      │
      ▼
scripts/cli.py   ← routines invoke these subcommands
      │
      ▼
guardrails (hard-coded FundedNext rules)
      │
      ▼
broker adapter:  MockBroker (Mac dev)  |  Mt5Broker (Windows)
      │
      ▼
MT5 terminal (Windows) or mock fixtures (Mac)
```

See `/Users/jericho/.claude/plans/1-2-step-expressive-puffin.md` for the full
design doc.

---

## Setup — Mac development

This repo was authored on macOS. The `MetaTrader5` Python package is
Windows-only, so local dev uses `MockBroker`. You can build, test, and
iterate on everything except real MT5 integration.

```bash
# Clone + install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: leave BROKER_MODE=mock, TRADING_STAGE=dev for now.

# Run tests
pytest

# Try a routine command end to end
python -m scripts.cli preflight
python -m scripts.cli snapshot --note "dev smoke"
python -m scripts.cli research --query "EURUSD London session bias"
```

---

## Setup — Windows (the real deal)

This is where MT5 actually runs. Do this on your Windows PC or a Windows VPS.

### 1. Install dependencies

```powershell
# Python 3.11+
python --version

# Clone repo
git clone <your-github-url> trading-routine
cd trading-routine

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-windows.txt
```

### 2. Install MetaTrader 5 + log into FundedNext

- Download MT5 from MetaQuotes or use FundedNext's direct link.
- Log in to your FundedNext **demo** account (MT5 server is usually
  `FundedNext-Demo` or similar — check your welcome email).
- Enable algorithmic trading: `Tools → Options → Expert Advisors →
  Allow algorithmic trading`.
- In the Market Watch panel, right-click → Show All. Verify the symbol names
  for EURUSD, XAUUSD, NAS100, etc. Some FundedNext servers use suffixes like
  `.r` — if so, edit `config/instruments.yml` `mt5_symbol` fields.

### 3. Configure .env

```powershell
copy .env.example .env
notepad .env
```

Fill in:
- `TRADING_STAGE=demo`
- `BROKER_MODE=mt5`
- `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` — from FundedNext dashboard
- `MT5_TERMINAL_PATH` — optional, path to `terminal64.exe`
- `PERPLEXITY_API_KEY` — [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api)
- `DISCORD_WEBHOOK_URL` — Server Settings → Integrations → Webhooks
- `ALLOW_LIVE_ORDERS=1` — only once you're confident the agent is behaving

### 4. Smoke test the connection

```powershell
python -m scripts.cli preflight
python -m scripts.cli positions
```

You should see your FundedNext demo balance and no positions.

### 5. Verify the FundedNext rule numbers

Open `config/fundednext.yml` and confirm:
- `daily_loss_limit_pct` matches your actual Stellar 2-step plan (usually 5%)
- `max_drawdown_pct` matches (usually 10%)
- Whitelist contains instruments your plan allows

**Verify on fundednext.com — rule numbers drift.**

### 6. Set up Claude Code routines

In Claude Code (desktop or CLI), open this project, then enable the routines
under `.claude/routines/` on the schedule documented in each file.

- On a Windows PC: Claude Code desktop app → Routines tab → New routine → paste the contents of `.claude/routines/<name>.md` and the cron from its header.
- On a Windows VPS: same, OR use Windows Task Scheduler to run
  `claude -p "<prompt file>"` on schedule.

### 7. Run 2 weeks on demo

Let it run. Check Discord. Review `memory/daily-journal/*.md` and
`memory/weekly-reviews/*.md`.

### 8. Promote to challenge (buy FundedNext 2-step)

```powershell
python -m scripts.promote --to challenge
```

It will refuse if demo history is too short or shows violations. It prompts
twice before flipping `TRADING_STAGE`.

---

## Project layout

```
.claude/routines/       # Claude Code routine prompts
.claude/settings.json   # Permissions for routines
scripts/                # Python — broker, guardrails, trade, journal, research
  broker/
    base.py             # Broker Protocol + dataclasses
    mock_broker.py      # Mac dev + CI
    mt5_broker.py       # Windows only — wraps MetaTrader5 pkg
  guardrails.py         # THE safety layer — read before changing anything
  account.py            # Equity curve + DD math
  trade.py              # Place/modify/close — passes through guardrails
  cli.py                # Entry point for routines
  promote.py            # Stage promotion (human-operated)
config/
  fundednext.yml        # Prop firm rules — verify against FundedNext site
  instruments.yml       # Symbol specs — fill in broker-specific naming on Windows
  sessions.yml          # Market hours per asset class
memory/                 # Agent's "personality" + state
  strategy.md           # How the agent trades (human-edited)
  playbook.md           # Allowed setups (human-edited)
  equity-curve.jsonl    # Source of truth for drawdown
  trade-log.jsonl       # All order / modify / close / reject events
  daily-journal/        # Agent's daily notes
  weekly-reviews/       # Friday gradings
tests/                  # Guardrail + account + broker tests (run on Mac, no MT5 needed)
```

---

## Development workflow

1. Make changes on Mac.
2. `pytest` — must pass with ≥ 80% coverage.
3. Commit, push.
4. On Windows: `git pull`, `pip install -r requirements-windows.txt` if deps
   changed, restart the Claude Code routines if behavior changed.

Never edit `config/fundednext.yml`, `.env`, or `scripts/guardrails.py` from
within a Claude Code routine. Those are human-only.

---

## Safety contract

This is experimental software. Prop firm accounts can be blown. Real money
can be lost once you promote past demo. The guardrail layer is defense in
depth, not a guarantee.

- Run on demo for at least 2 weeks.
- Review every daily journal entry.
- If anything looks wrong, `python -m scripts.cli flatten --reason "manual"`
  and investigate.
- The `promote.py` script is the only path to real money. Keep it that way.

---

## License

Private repo. Not financial advice. Not affiliated with FundedNext, MetaQuotes,
Anthropic, or Perplexity.
