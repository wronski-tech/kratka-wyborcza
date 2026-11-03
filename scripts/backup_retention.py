#!/usr/bin/env python3
"""Which backups to delete under a daily / weekly / monthly retention (default 7 / 4 / 6).

Reads backup file names (``plujka-YYYYmmdd-HHMMSS.dump``) on stdin and prints the ones to delete. The newest
backup of each of the last ``--daily`` days that have a backup is kept, likewise per ISO week and per month;
a backup kept by any rule stays. Periods are counted among existing backups, not on the calendar, so when
backups stop being made, nothing more is deleted. Other names are ignored (never deleted).

  ssh -p 23 u1@u1.your-storagebox.de ls -1 plujka | python3 scripts/backup_retention.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections.abc import Callable, Iterable

NAME = re.compile(r"^plujka-(\d{8})-(\d{6})\.dump$")


def to_delete(names: Iterable[str], daily: int = 7, weekly: int = 4, monthly: int = 6) -> list[str]:
    """Names of the backups no rule keeps, oldest first."""
    stamped = sorted(((dt.datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S"), name)
                      for name in names if (m := NAME.match(name.strip()))), reverse=True)
    rules: list[tuple[int, Callable[[dt.datetime], tuple]]] = [
        (daily, lambda t: (t.date(),)),
        (weekly, lambda t: tuple(t.isocalendar()[:2])),
        (monthly, lambda t: (t.year, t.month)),
    ]
    keep: set[str] = set()
    for count, period in rules:
        seen: list[tuple] = []
        for stamp, name in stamped:  # newest first: the first backup of a period is its newest
            if period(stamp) not in seen and len(seen) < count:
                seen.append(period(stamp))
                keep.add(name)
    return [name for _, name in reversed(stamped) if name not in keep]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--daily", type=int, default=7)
    parser.add_argument("--weekly", type=int, default=4)
    parser.add_argument("--monthly", type=int, default=6)
    args = parser.parse_args()
    print("\n".join(to_delete(sys.stdin.read().split(), args.daily, args.weekly, args.monthly)))


if __name__ == "__main__":
    main()
