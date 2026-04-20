"""Phase 3: LLM-in-loop filter over the portfolio backtest.

Reads the 619-trade portfolio_trades.csv (output of run_portfolio), replays
each trade's pre-entry context through Claude Sonnet scoring the 5-check
rubric, and recomputes stats for the LLM-filtered subset.

For each trade:
  - Load 20 M15 bars preceding the signal time on that symbol
  - Derive HTF close state (prior-day H/L, recent range) from those bars
  - Format a structured context prompt
  - Ask Claude to score the rubric (1-5) and return A / B / skip

Default filter:
  - A (5/5 rubric): kept, treated as A-grade (1% risk in live; 2x R in backtest)
  - B (4/5): kept at standard 0.5% risk
  - skip (<4/5 or playbook-match=false): dropped

Output:
  memory/backtest-results/portfolio-1y-llm/trades_with_llm.csv
  - Original columns + llm_grade + llm_score + llm_reason

Then run `compare_mechanical_vs_llm()` to print:
  Mechanical baseline (unfiltered): X trades, Y% WR, Z avg R, W% return
  LLM-filtered (>= B-grade):       X trades, Y% WR, Z avg R, W% return
  A-grade cohort:                  X trades, etc.
  B-grade cohort:                  X trades, etc.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .broker.base import Bar, Timeframe


SONNET_MODEL = "claude-sonnet-4-5-20250929"
CONTEXT_BARS = 20
API_SLEEP_SECS = 0.5   # be polite to the API


# ---------- Data ----------

@dataclass(frozen=True)
class TradeRow:
    fill_time: datetime
    close_time: datetime
    symbol: str
    side: str
    setup: str
    r_multiple: float
    entry_balance: float


def load_trades(path: Path) -> list[TradeRow]:
    rows: list[TradeRow] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if not r["fill_time"] or not r["r_multiple"]:
                continue
            rows.append(TradeRow(
                fill_time=datetime.fromisoformat(r["fill_time"]),
                close_time=datetime.fromisoformat(r["close_time"]),
                symbol=r["symbol"],
                side=r["side"],
                setup=r["setup"],
                r_multiple=float(r["r_multiple"]),
                entry_balance=float(r["entry_balance"]),
            ))
    return rows


def load_bars_for_symbol(symbol: str, tf: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    from .backtest import load_mt5_history
    return load_mt5_history(symbol, tf, start, end)


# ---------- Context builder ----------

def build_context(trade: TradeRow, bars: list[Bar], n: int = CONTEXT_BARS) -> dict:
    """Return structured context for the prompt: last n bars before signal + derived stats."""
    prior = [b for b in bars if b.time < trade.fill_time]
    window = prior[-n:] if len(prior) >= n else prior
    if not window:
        return {}
    last = window[-1]
    recent_range = max(b.high for b in window) - min(b.low for b in window)
    # Previous-day high/low (UTC)
    trade_date = trade.fill_time.date()
    prev_day = [b for b in prior if b.time.date() < trade_date]
    pd_hi = max((b.high for b in prev_day[-24:]), default=last.close)
    pd_lo = min((b.low for b in prev_day[-24:]), default=last.close)
    return {
        "symbol": trade.symbol,
        "side": trade.side,
        "setup": trade.setup,
        "signal_time_utc": trade.fill_time.isoformat(),
        "last_close": round(last.close, 5),
        "recent_range": round(recent_range, 5),
        "prior_day_high": round(pd_hi, 5),
        "prior_day_low": round(pd_lo, 5),
        "last_n_bars": [
            {"t": b.time.isoformat(timespec="minutes"),
             "o": round(b.open, 5), "h": round(b.high, 5),
             "l": round(b.low, 5), "c": round(b.close, 5)}
            for b in window
        ],
    }


# ---------- Claude scorer ----------

SYSTEM_PROMPT = """You are a disciplined FX/indices trader scoring trade setups against a fixed rubric.

The rubric has 5 checks. Score each True/False based ONLY on the pre-entry context provided:

1. **Matches a specific playbook setup** — the setup tag is given; assume TRUE if tag is one of: london_breakout, ny_momentum, gold_pullback, failed_breakout_fade
2. **HTF trend aligned** — does the recent bar momentum agree with the trade direction? For BUY: last close should be trending up (higher highs in last bars). For SELL: trending down.
3. **Clear of red-folder news** — without a news feed, assume TRUE unless the signal time is within 30 min of a known major event. Since you don't have live news, default TRUE.
4. **R:R ≥ 2.0** — assume TRUE; the setups are designed for 2R targets.
5. **At a meaningful level** — is the signal price near a prior high/low, recent range boundary, or round number? TRUE if within 0.5% of prior_day_high/low or within the upper/lower 20% of recent_range.

Grading:
- 5/5 TRUE → grade="A"
- 4/5 TRUE (with item 1 TRUE) → grade="B"
- <4/5 OR item 1 FALSE → grade="skip"

Be HONEST. Do NOT rubber-stamp. If the setup is directionally weak (e.g., BUY against strong downtrend in last bars), score item 2 FALSE and the grade becomes "skip".

Return ONLY a JSON object with this shape — no prose before or after:
{
  "item1": true|false,
  "item2": true|false,
  "item3": true|false,
  "item4": true|false,
  "item5": true|false,
  "grade": "A"|"B"|"skip",
  "reason": "one sentence"
}
"""


def score_trade(client, context: dict) -> dict:
    """Call Claude with the context and return a parsed score dict."""
    from anthropic import Anthropic
    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=400,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": "Score this trade setup:\n\n" + json.dumps(context, indent=2),
        }],
    )
    text = resp.content[0].text.strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Return skip for any parse failure
        return {"grade": "skip", "reason": f"parse_error: {text[:80]}", "score": 0}
    score = sum(1 for k in ("item1", "item2", "item3", "item4", "item5") if parsed.get(k) is True)
    parsed["score"] = score
    return parsed


# ---------- Main loop ----------

def run_llm_filter(
    trades_csv: Path, output_dir: Path, *,
    start: datetime, end: datetime, limit: int | None = None,
) -> Path:
    from anthropic import Anthropic
    try:
        from dotenv import load_dotenv; load_dotenv()
    except ImportError:
        pass
    client = Anthropic()

    trades = load_trades(trades_csv)
    if limit is not None:
        trades = trades[:limit]
    print(f"[llm] scoring {len(trades)} trades via {SONNET_MODEL}...", file=sys.stderr)

    # Group by (symbol, setup) to determine which timeframe + bars to load
    setup_tf = {
        "london_breakout": Timeframe.M15,
        "ny_momentum": Timeframe.M15,
        "gold_pullback": Timeframe.H1,
        "failed_breakout_fade": Timeframe.H4,
    }
    # Cache bars per (symbol, tf)
    bar_cache: dict[tuple[str, Timeframe], list[Bar]] = {}

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "trades_with_llm.csv"
    input_tokens_total = 0
    output_tokens_total = 0

    with out_path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow([
            "fill_time", "close_time", "symbol", "side", "setup",
            "r_multiple", "entry_balance", "llm_grade", "llm_score", "llm_reason",
        ])

        for i, t in enumerate(trades):
            tf = setup_tf.get(t.setup, Timeframe.M15)
            cache_key = (t.symbol, tf)
            if cache_key not in bar_cache:
                print(f"[llm] loading bars for {t.symbol} {tf.value}...", file=sys.stderr)
                bar_cache[cache_key] = load_bars_for_symbol(t.symbol, tf, start, end)
            bars = bar_cache[cache_key]
            ctx = build_context(t, bars)
            if not ctx:
                writer.writerow([
                    t.fill_time.isoformat(), t.close_time.isoformat(),
                    t.symbol, t.side, t.setup, f"{t.r_multiple:.4f}",
                    f"{t.entry_balance:.2f}", "skip", 0, "no_context",
                ])
                continue

            try:
                result = score_trade(client, ctx)
            except Exception as e:
                result = {"grade": "skip", "reason": f"api_error: {type(e).__name__}", "score": 0}

            writer.writerow([
                t.fill_time.isoformat(), t.close_time.isoformat(),
                t.symbol, t.side, t.setup, f"{t.r_multiple:.4f}",
                f"{t.entry_balance:.2f}",
                result.get("grade", "skip"),
                result.get("score", 0),
                (result.get("reason") or "")[:200],
            ])
            out.flush()

            if (i + 1) % 20 == 0:
                print(f"[llm] {i+1}/{len(trades)} scored", file=sys.stderr)
            time.sleep(API_SLEEP_SECS)

    print(f"[llm] done. saved to {out_path}", file=sys.stderr)
    return out_path


# ---------- Comparison ----------

def compare(trades_with_llm_csv: Path, initial_balance: float = 50_000.0, risk_pct: float = 0.5) -> str:
    rows: list[dict] = []
    with trades_with_llm_csv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    def cohort_stats(filtered_rows: list[dict], a_grade_doubles: bool = False) -> dict:
        bal = initial_balance
        peak = bal
        max_dd = 0.0
        wins = 0
        losses = 0
        rs: list[float] = []
        for r in sorted(filtered_rows, key=lambda x: x["fill_time"]):
            rm = float(r["r_multiple"])
            entry_bal = float(r["entry_balance"])
            # Optional: double risk for A-grade
            rr = risk_pct * (2.0 if a_grade_doubles and r.get("llm_grade") == "A" else 1.0)
            bal += rm * entry_bal * (rr / 100.0)
            peak = max(peak, bal)
            dd = 100.0 * (peak - bal) / max(peak, 1e-9)
            max_dd = max(max_dd, dd)
            rs.append(rm)
            if rm > 0: wins += 1
            else: losses += 1
        n = len(rs)
        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": 100.0 * wins / max(n, 1),
            "avg_r": sum(rs) / max(n, 1),
            "total_r": sum(rs),
            "final_balance": round(bal, 2),
            "return_pct": round(100.0 * (bal - initial_balance) / initial_balance, 2),
            "max_dd_pct": round(max_dd, 2),
        }

    mechanical = cohort_stats(rows)
    llm_ab = cohort_stats([r for r in rows if r["llm_grade"] in ("A", "B")])
    llm_ab_with_sizing = cohort_stats(
        [r for r in rows if r["llm_grade"] in ("A", "B")], a_grade_doubles=True,
    )
    a_only = cohort_stats([r for r in rows if r["llm_grade"] == "A"])
    b_only = cohort_stats([r for r in rows if r["llm_grade"] == "B"])
    skips = cohort_stats([r for r in rows if r["llm_grade"] == "skip"])

    def fmt(name: str, s: dict) -> str:
        return (
            f"  {name:35s} n={s['n']:4d}  WR={s['win_rate_pct']:5.1f}%  "
            f"avgR={s['avg_r']:+.3f}  totR={s['total_r']:+7.2f}  "
            f"final=${s['final_balance']:>10,.2f}  ({s['return_pct']:+6.2f}%)  "
            f"maxDD={s['max_dd_pct']:5.2f}%"
        )

    return "\n".join([
        "=" * 100,
        "  LLM FILTER COMPARISON",
        "=" * 100,
        fmt("Mechanical (unfiltered baseline)", mechanical),
        fmt("LLM-filtered (A+B, flat 0.5%)", llm_ab),
        fmt("LLM-filtered (A+B, A=1% sizing)", llm_ab_with_sizing),
        fmt("  of which A-grade only", a_only),
        fmt("  of which B-grade only", b_only),
        fmt("SKIPPED (if taken, would have been)", skips),
        "=" * 100,
    ])


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.backtest_llm")
    parser.add_argument("--trades", required=True, help="portfolio_trades.csv input")
    parser.add_argument("--output", required=True, help="output dir")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="optional cap on trades (for dry runs)")
    parser.add_argument("--compare-only", action="store_true", help="skip API calls, compare existing trades_with_llm.csv")
    args = parser.parse_args(argv)

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    output_dir = Path(args.output)

    if args.compare_only:
        csv_path = output_dir / "trades_with_llm.csv"
    else:
        csv_path = run_llm_filter(
            trades_csv=Path(args.trades), output_dir=output_dir,
            start=start, end=end, limit=args.limit,
        )

    print(compare(csv_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
