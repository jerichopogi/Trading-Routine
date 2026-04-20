# Windows Setup Guide

End-to-end setup for running Trading Routine on Windows with live MT5. From a fresh machine to a fully autonomous agent placing demo trades on FundedNext / MetaQuotes-Demo.

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Install Python + the repo](#2-install-python--the-repo)
3. [Configure `.env`](#3-configure-env)
4. [Set up the MT5 terminal](#4-set-up-the-mt5-terminal)
5. [Verify the installation](#5-verify-the-installation)
6. [Install Claude Code (CLI + Desktop)](#6-install-claude-code-cli--desktop)
7. [Set up autonomous scheduling](#7-set-up-autonomous-scheduling)
8. [Run backtests](#8-run-backtests)
9. [Troubleshooting](#9-troubleshooting)
10. [Safety checklist before going live](#10-safety-checklist-before-going-live)

---

## 1. Prerequisites

| Required | Why | Notes |
|---|---|---|
| Windows 10/11 | MT5 is Windows-only | Linux/Mac can run research + backtest but cannot place MT5 orders |
| Python 3.12+ | Runtime | Installer from [python.org](https://www.python.org/downloads/) — check "Add to PATH" |
| MetaTrader 5 terminal | Broker | [Install from MetaQuotes](https://www.metatrader5.com/en/download) or your prop firm |
| Broker account | Execution | FundedNext demo, MetaQuotes demo, or any MT5 broker |
| Discord webhook (optional) | Notifications | Create one in a Discord channel → Integrations → Webhooks |
| Perplexity API key (optional) | Research routine | [perplexity.ai/account/api](https://www.perplexity.ai/account/api) |
| Anthropic API key (optional) | Phase-3 backtest | [console.anthropic.com](https://console.anthropic.com) — only needed for LLM-in-loop backtest |
| git | Cloning | [git-scm.com](https://git-scm.com/) or via GitHub Desktop |

Total setup time: **~30–45 minutes** first time.

---

## 2. Install Python + the repo

### 2.1 Install Python

1. Download Python 3.12 or 3.13 from [python.org/downloads](https://www.python.org/downloads/).
2. Run the installer. **Check "Add Python to PATH"** on the first screen. Otherwise subsequent steps fail with *"python is not recognized"*.
3. Verify in a **new** PowerShell or Command Prompt window:

```powershell
python --version
# Python 3.12.x or 3.13.x
```

### 2.2 Clone the repo

```powershell
cd $HOME\Documents
git clone https://github.com/<your-fork>/Trading-Routine.git
cd Trading-Routine
```

Or download the ZIP from GitHub and extract to a folder.

### 2.3 Create a virtual environment (recommended)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# (or) .\.venv\Scripts\activate.bat from cmd
```

You should see `(.venv)` at the start of your prompt.

If PowerShell blocks activation, run once as admin:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 2.4 Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-windows.txt
```

`requirements-windows.txt` adds `MetaTrader5` which is Windows-only. If this fails, make sure you're on Windows with Python 3.12/3.13 (older versions may not have wheels available).

---

## 3. Configure `.env`

Copy the example and fill in your values:

```powershell
Copy-Item .env.example .env
notepad .env
```

Minimum for live MT5 execution:

```ini
# Stage: dev | demo | challenge | funded
# - dev = MockBroker (no real orders)
# - demo = MT5 broker, demo account
TRADING_STAGE=demo
BROKER_MODE=mt5

# MT5 credentials (from the broker's email / app)
MT5_LOGIN=12345678
MT5_PASSWORD=your-password-here
MT5_SERVER=MetaQuotes-Demo
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

# Safety: 1 allows real orders, 0 = dry-run only
ALLOW_LIVE_ORDERS=1

# Optional — research + notifications
PERPLEXITY_API_KEY=pplx-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Display timezone (what you see in journal + Discord)
DISPLAY_TIMEZONE=America/New_York

# Initial balance for rule-status calcs (must match real account)
INITIAL_BALANCE=50000
```

**Never commit `.env`.** It's in `.gitignore`.

---

## 4. Set up the MT5 terminal

1. Launch MT5: `C:\Program Files\MetaTrader 5\terminal64.exe`.
2. **File → Login to Trade Account** → enter your broker credentials.
3. Wait for the green "connected" indicator at the bottom right.
4. Verify account details: **View → Terminal → Trade tab** — your balance should show.

### 4.1 Enable Algo Trading

The routines place orders programmatically; MT5 blocks this by default.

1. **Top toolbar**: click the **"Algo Trading"** button. It should turn **green**. Keyboard shortcut: `Ctrl+E`.
2. **Tools → Options → Expert Advisors** → ensure ☑ **"Allow algorithmic trading"** is checked → OK.
3. Re-click the Algo Trading toolbar button once more to confirm state.

If the button is red, orders will fail with `retcode=10026 AutoTrading disabled by server`.

### 4.2 Load the symbols you plan to trade

MT5 shows only active symbols in Market Watch by default. To trade EURUSD, XAUUSD, etc., add them:

**Right-click Market Watch → Symbols** → find each symbol → click **Show**:

- FX: EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD
- Metals: XAUUSD, XAGUSD
- Indices (MetaQuotes-Demo names): USTEC (NASDAQ), US30 (Dow), US500 (S&P), GER40 (DAX)
- Indices (FundedNext names): NAS100, US30, SPX500, GER40

Different brokers use different symbol names. Check what your broker has.

---

## 5. Verify the installation

Open PowerShell in the repo root (venv activated):

```powershell
# Preflight: reads real account, checks rules
python -m scripts.cli preflight --json
```

Expected output:

```json
{
  "stage": "demo",
  "server": "MetaQuotes-Demo",
  "balance": 50000.0,
  "equity": 50000.0,
  "open_positions": 0,
  "daily_dd_pct": 0.0,
  "max_dd_pct": 0.0,
  "firm_violation": false,
  "hard_stop_hit": false,
  "reasons": [],
  "tradeable_symbols": ["EURUSD", "GBPUSD", ...]
}
```

If you see this, the pipeline is wired. Also try:

```powershell
python -m scripts.cli snapshot --note "first test"
python -m scripts.cli positions --json
python -m scripts.cli session-status EURUSD
```

---

## 6. Install Claude Code (CLI + Desktop)

### 6.1 CLI

```powershell
winget install Anthropic.ClaudeCode
```

Or download from [claude.com/code](https://claude.com/code). Verify:

```powershell
claude --version
```

Log in once:

```powershell
claude login
```

### 6.2 Desktop app

Download from [claude.com/download](https://claude.com/download). Install and sign in with the same account as the CLI.

This gives you the **Schedule** sidebar for autonomous routines.

---

## 7. Set up autonomous scheduling

Two categories of scheduling:

- **Claude Desktop local scheduled tasks** — drive the LLM-in-loop routines (pre-session, session-open, etc.). Require the PC to be on and the Desktop app running.
- **Windows Task Scheduler** — drives the mechanical `manage-runners` job (pure Python, no LLM).

### 7.1 Desktop scheduled tasks (7 tasks)

All tasks use:
- **Working folder**: `<path>\Trading-Routine`
- **Model**: Sonnet (Opus only for weekly-review)
- **Permission mode**: Accept edits
- **Worktree toggle**: OFF

| Task | Prompt | Frequency | Local time (UTC+8) |
|---|---|---|---|
| pre-session-london | `@.claude/routines/pre-session.md` (see README for fuller prompt) | Weekdays | 14:45 |
| session-open-london | `@.claude/routines/session-open.md` | Weekdays | 15:05 |
| pre-session-ny | `@.claude/routines/pre-session.md` (NY-focused prompt) | Weekdays | 21:00 |
| session-open-ny | `@.claude/routines/session-open.md` | Weekdays | 21:35 |
| midday-breakeven | `@.claude/routines/midday.md` | Daily | 01:00 |
| session-close-ny | `@.claude/routines/session-close.md` | Daily | 05:00 |
| weekly-review | `@.claude/routines/weekly-review.md` (model: Opus) | Weekly → Saturday | 06:00 |

Convert local time for your timezone as needed. UTC reference times are in the repo's root README.

**Important**: Enable "Keep computer awake" under **Claude Desktop → Settings → Desktop app → General** — sleeping skips tasks.

### 7.2 Windows Task Scheduler — `manage-runners`

This is the every-15-minutes mechanical job: breakeven SL, partial TPs at 2R/3R, trailing SL above 3R. Not an LLM session — just Python.

**Via PowerShell (recommended, reproducible):**

```powershell
$repoPath = "C:\Users\jeric\Documents\Trading-Routine"  # adjust

$action  = New-ScheduledTaskAction `
  -Execute "$repoPath\run-manage.cmd"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "15:00:00") `
  -RepetitionInterval (New-TimeSpan -Minutes 15) `
  -RepetitionDuration (New-TimeSpan -Days (365*10))

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "trading-manage-runners" `
  -Action $action -Trigger $trigger -Settings $settings `
  -Description "Breakeven, partial TPs, trailing SL every 15 min"
```

**Via GUI:**

Open Task Scheduler → Create Task → set:
- **General**: Name `trading-manage-runners`, "Run only when user is logged on"
- **Triggers → New**: Daily at any time, ☑ Repeat task every `15 minutes` for duration `1 day`
- **Actions → New**: Start a program → `C:\Users\jeric\Documents\Trading-Routine\run-manage.cmd`
- **Conditions**: uncheck AC-power requirements
- **Settings**: "Do not start a new instance" if already running

The launcher `run-manage.cmd` is already in the repo (gitignored — paths are machine-specific). It cd's, activates nothing (uses system Python), runs `python -m scripts.cli manage-runners`, and logs to `logs\manage-runners-YYYY-MM-DD.log`.

---

## 8. Run backtests

### 8.1 Single-setup backtest

```powershell
# London Breakout on EURUSD, 1 year
python -m scripts.backtest --from 2025-04-21 --to 2026-04-21 `
  --output memory\backtest-results\eurusd-1y

# NY Momentum on USTEC
python -m scripts.backtest --symbol USTEC --setup ny_momentum `
  --from 2025-04-21 --to 2026-04-21 --pip-size 1.0

# Gold Pullback on XAUUSD H1
python -m scripts.backtest --symbol XAUUSD --setup gold_pullback `
  --timeframe H1 --from 2025-04-21 --to 2026-04-21

# FX Failed-Breakout Fade on GBPUSD H4
python -m scripts.backtest --symbol GBPUSD --setup failed_breakout_fade `
  --timeframe H4 --from 2025-04-21 --to 2026-04-21
```

### 8.2 Full portfolio backtest

```powershell
python -m scripts.backtest --portfolio `
  --from 2025-04-21 --to 2026-04-21 `
  --output memory\backtest-results\portfolio-1y
```

Writes `portfolio_trades.csv` and `portfolio_stats.json`.

### 8.3 Phase-3 LLM-in-loop filter

Requires `ANTHROPIC_API_KEY` in `.env`. Costs ~$3 via Sonnet.

```powershell
python -m scripts.backtest_llm `
  --trades memory\backtest-results\portfolio-1y\portfolio_trades.csv `
  --output memory\backtest-results\portfolio-1y-llm `
  --from 2025-04-21 --to 2026-04-21

# Re-analyze without new API calls:
python -m scripts.backtest_llm ... --compare-only

# Dry run (5 trades, ~$0.03):
python -m scripts.backtest_llm ... --limit 5
```

---

## 9. Troubleshooting

### `MT5 initialize failed: (-10005, 'IPC timeout')`

- MT5 terminal is not running. Launch `terminal64.exe` and log in.
- If MT5 is running but getting this, close and reopen it — some builds lock IPC on sleep/wake.

### `MT5 initialize failed: (..., 'Cannot select symbol X')`

- Your broker doesn't have that symbol name. Open Market Watch in MT5 → right-click → Symbols → search for equivalent (e.g., `NAS100` might be `USTEC` on MetaQuotes-Demo).
- Use `--symbol USTEC` (or whatever the real name is) in backtest calls.

### `retcode=10026 AutoTrading disabled by server`

Covered in [4.1](#41-enable-algo-trading). Toggle the green Algo Trading button in the toolbar. If still blocked after both toggles, it's a broker-side restriction — switch to MetaQuotes-Demo or ask the broker.

### `retcode=10030 Unsupported filling mode`

Fixed in the adapter (auto-picks FOK / IOC / RETURN). If you see it, ensure you're on the latest `main` branch where `_pick_filling()` is present in `scripts/broker/mt5_broker.py`.

### Routine sees no ideas / `0 trades placed`

Pre-session didn't journal any ideas. Check `memory/daily-journal/<today>.md` — if it's empty, the pre-session routine either didn't run, didn't find catalysts, or the journal write failed. Look at the Claude Desktop scheduled-task history for the pre-session run.

### `PERPLEXITY_API_KEY not set`

You're running Claude Desktop in the cloud, not local. The cloud sandbox doesn't have your `.env`. Either:
- Move the routine to a **local** Desktop task (not remote/cloud), OR
- Paste the key via Claude Desktop's cloud-environment variables UI

See the README's "Environment split" section.

### Python import errors on `MetaTrader5`

`pip install MetaTrader5` may fail on Python 3.13+ on some releases. If so, install Python 3.12 instead and recreate the venv.

### Windows Task Scheduler runs but nothing happens

- Check `logs\manage-runners-YYYY-MM-DD.log` for errors.
- Ensure the task's "Run only when user is logged on" is set (the MT5 terminal runs under your user session — service-mode tasks can't see it).
- Verify the `run-manage.cmd` file exists and points at the right repo path.

### `claude` command not found in Task Scheduler context

If your scheduled task runs `claude` directly, it may fail because Task Scheduler doesn't inherit your PATH. Use `run-routine.cmd` which invokes `claude.cmd` from PATH, or hard-code the full path to `claude.exe` in the task action.

---

## 10. Safety checklist before going live

### Demo → challenge transition

- [ ] `TRADING_STAGE=demo` verified, ran ≥2 weeks on demo with real metrics ≥ 70% of backtest
- [ ] Manually inspect `memory/trade-log.jsonl` after one full week — trades make sense, guardrail rejections are correct
- [ ] Discord pings arrive for every order event
- [ ] Session-close correctly flattens at EOD
- [ ] Weekly review generates a graded report
- [ ] `manage-runners` Task Scheduler job logs show BE moves and partials firing at expected times
- [ ] FundedNext challenge account balance verified — set `INITIAL_BALANCE` to match

### Before flipping to live

- [ ] Set `TRADING_STAGE=challenge` or `funded` in `.env`
- [ ] Reduce `risk.per_trade_risk_pct` in `config/fundednext.yml` if you want tighter than 0.5% — **you cannot raise it past the ceiling**; guardrails enforce
- [ ] Re-run preflight and confirm server name matches the challenge/funded account
- [ ] Have a manual "kill switch" plan: if something goes wrong, you know how to
  - Flatten all: `python -m scripts.cli flatten --reason "manual kill"`
  - Disable Algo Trading in MT5 (Ctrl+E)
  - Unregister the Task Scheduler job: `Unregister-ScheduledTask -TaskName trading-manage-runners`
- [ ] Know the FundedNext rules you're bound by — daily 5% / overall 10% DD. Guardrails have an internal 4% / 8% cushion, but there's no shield if the agent hits two setups back to back on a volatile news day

### Daily checklist (first 2 weeks live)

- [ ] Glance at Discord morning summary
- [ ] Manually check MT5 Trade tab for any positions you didn't expect
- [ ] Review `memory/daily-journal/<today>.md` to see what the LLM decided
- [ ] Check `logs/manage-runners-*.log` for errors

---

## Where to look for more

- **Project README** (`README.md`): high-level overview, architecture, routine cron times in UTC
- **CLAUDE.md**: instructions for the AI agent, not for you
- **`memory/strategy.md`**: the trader's constitution
- **`memory/playbook.md`**: the 4 playable setups
- **`.claude/routines/*.md`**: individual routine specs
- **Backtest results**: `memory/backtest-results/portfolio-1y/` (committed CSVs from the last backtest run)
