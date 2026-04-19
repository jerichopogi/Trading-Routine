"""CLI helpers the routines invoke from the shell.

Examples:
    python -m scripts.cli preflight
    python -m scripts.cli snapshot
    python -m scripts.cli positions
    python -m scripts.cli flatten --reason "end of day"
    python -m scripts.cli breakeven
    python -m scripts.cli session-status EURUSD
    python -m scripts.cli research --query "EURUSD London catalysts next 12h"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from . import decide, journal, notify, research, sessions, trade
from .account import append_snapshot, snapshot
from .broker import get_broker


def _stage() -> str:
    return os.environ.get("TRADING_STAGE", "dev")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def cmd_preflight(args: argparse.Namespace) -> int:
    broker = get_broker()
    broker.connect()
    try:
        report = decide.preflight(broker, stage=_stage())
    finally:
        broker.disconnect()
    if args.json:
        d = asdict(report)
        print(json.dumps(d, indent=2, default=str))
    else:
        print(report.summary())
    if report.hard_stop_hit or report.firm_violation:
        return 3
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    broker = get_broker()
    broker.connect()
    try:
        snap = snapshot(broker, stage=_stage(), note=args.note or "")
        append_snapshot(snap)
    finally:
        broker.disconnect()
    print(snap.to_json())
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    broker = get_broker()
    broker.connect()
    try:
        positions = broker.positions()
    finally:
        broker.disconnect()
    if args.json:
        print(json.dumps([asdict(p) for p in positions], indent=2, default=str))
    else:
        if not positions:
            print("(no open positions)")
        for p in positions:
            print(
                f"#{p.ticket} {p.symbol} {p.side.value} vol={p.volume} "
                f"@ {p.price_open} sl={p.sl} tp={p.tp} pnl={p.profit:+.2f} "
                f"r={p.r_multiple:.2f}" if p.r_multiple is not None else
                f"#{p.ticket} {p.symbol} {p.side.value} vol={p.volume} "
                f"@ {p.price_open} sl={p.sl} tp={p.tp} pnl={p.profit:+.2f}"
            )
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    broker = get_broker()
    broker.connect()
    try:
        closed = trade.flatten_all(broker, reason=args.reason, stage=_stage())
    finally:
        broker.disconnect()
    print(f"Closed {len(closed)} positions: {closed}")
    return 0


def cmd_breakeven(args: argparse.Namespace) -> int:
    broker = get_broker()
    broker.connect()
    try:
        moved = trade.tighten_stops_to_breakeven(broker, min_r=args.min_r)
    finally:
        broker.disconnect()
    print(f"Moved {moved} stops to breakeven (min {args.min_r}R)")
    return 0


def cmd_session_status(args: argparse.Namespace) -> int:
    st = sessions.status(args.symbol)
    print(json.dumps(asdict(st), indent=2))
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    result = research.ask(args.query)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
        return 0 if result.ok else 1
    print(result.answer if result.ok else f"ERROR: {result.error}")
    if result.citations:
        print("\nCitations:")
        for c in result.citations:
            print(f"- {c}")
    return 0 if result.ok else 1


def cmd_journal(args: argparse.Namespace) -> int:
    if args.daily:
        path = journal.append_daily_note(section=args.section, body=args.body)
    else:
        path = journal.append_weekly_note(section=args.section, body=args.body)
    print(f"Wrote to {path}")
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    fn = {"info": notify.info, "warn": notify.warn, "error": notify.error, "success": notify.success}[args.level]
    ok = fn(args.title, args.body)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    sn = sub.add_parser("snapshot")
    sn.add_argument("--note", default="")
    sn.set_defaults(func=cmd_snapshot)

    pos = sub.add_parser("positions")
    pos.add_argument("--json", action="store_true")
    pos.set_defaults(func=cmd_positions)

    fl = sub.add_parser("flatten")
    fl.add_argument("--reason", required=True)
    fl.set_defaults(func=cmd_flatten)

    be = sub.add_parser("breakeven")
    be.add_argument("--min-r", type=float, default=1.0, dest="min_r")
    be.set_defaults(func=cmd_breakeven)

    ss = sub.add_parser("session-status")
    ss.add_argument("symbol")
    ss.set_defaults(func=cmd_session_status)

    rs = sub.add_parser("research")
    rs.add_argument("--query", required=True)
    rs.add_argument("--json", action="store_true")
    rs.set_defaults(func=cmd_research)

    jr = sub.add_parser("journal")
    jr.add_argument("--daily", action="store_true", help="daily journal (default: weekly)")
    jr.add_argument("--section", required=True)
    jr.add_argument("--body", required=True)
    jr.set_defaults(func=cmd_journal)

    nt = sub.add_parser("notify")
    nt.add_argument("--level", choices=["info", "success", "warn", "error"], default="info")
    nt.add_argument("--title", required=True)
    nt.add_argument("--body", default="")
    nt.set_defaults(func=cmd_notify)

    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
