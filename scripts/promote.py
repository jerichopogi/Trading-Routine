"""Stage promotion: dev → demo → challenge → funded.

Interactive CLI. Refuses to promote if the equity curve shows violations or
too little history. Writes every attempt to promotion.log.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import notify
from .account import read_equity_curve

STAGES = ("dev", "demo", "challenge", "funded")
MIN_DEMO_DAYS = 14

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMOTION_LOG = REPO_ROOT / "promotion.log"


def _log(msg: str) -> None:
    line = f"{datetime.now(UTC).isoformat()}  {msg}\n"
    with PROMOTION_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line.rstrip())


def _eligible(target: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    curve = read_equity_curve()

    if target == "demo":
        return True, reasons  # always ok to demo-trial

    if target in ("challenge", "funded"):
        if not curve:
            reasons.append("no equity curve — run demo first")
            return False, reasons
        span = curve[-1].ts - curve[0].ts
        if span < timedelta(days=MIN_DEMO_DAYS):
            reasons.append(
                f"only {span.days} days of equity history, need ≥ {MIN_DEMO_DAYS}"
            )
        flat = all(abs(s.equity - curve[0].equity) < 0.01 for s in curve)
        if flat:
            reasons.append("equity is flat — agent never traded")
    return len(reasons) == 0, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Promote trading stage")
    parser.add_argument("--to", required=True, choices=STAGES, dest="target")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    args = parser.parse_args(argv)

    ok, reasons = _eligible(args.target)
    if not ok:
        print("Cannot promote:")
        for r in reasons:
            print(f"  - {r}")
        _log(f"PROMOTION REJECTED to={args.target} reasons={reasons}")
        return 2

    print(f"About to promote stage to: {args.target}")
    print("This changes .env TRADING_STAGE. Real orders will be placed on next routine run.")
    if not args.yes:
        ans1 = input("Type the target stage to confirm: ").strip()
        if ans1 != args.target:
            print("Mismatch — aborting.")
            return 1
        ans2 = input("Type YES to proceed: ").strip()
        if ans2 != "YES":
            print("Aborted.")
            return 1

    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        print(".env does not exist. Copy .env.example first.")
        return 1

    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("TRADING_STAGE="):
            new_lines.append(f"TRADING_STAGE={args.target}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"TRADING_STAGE={args.target}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    _log(f"PROMOTED to={args.target}")
    notify.warn(
        title=f"Trading stage promoted → {args.target}",
        body=f"{datetime.now(UTC).isoformat()} — review routines before next trigger.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
