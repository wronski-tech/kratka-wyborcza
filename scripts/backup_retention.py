#!/usr/bin/env python3
"""Which backups to delete under a daily / weekly / monthly retention (default 7 / 4 / 6).

Reads backup file names on stdin and prints the ones to delete: database dumps (``plujka-YYYYmmdd-HHMMSS.dump``)
and archives of feedback and configuration (``plujka-files-YYYYmmdd-HHMMSS.tar.gz``), each series on its own;
an encrypted backup (``.age`` appended) belongs to the series of its plain name.
The newest backup of each of the last ``--daily`` days that have a backup is kept, likewise per ISO week and
per month; a backup kept by any rule stays. Periods are counted among existing backups, not on the calendar, so when
backups stop being made, nothing more is deleted. Other names are ignored (never deleted).

  ssh -p 23 u1@u1.your-storagebox.de ls -1 plujka | python3 scripts/backup_retention.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections.abc import Callable, Iterable

NAME = re.compile(r"^(plujka-files|plujka)-(\d{8})-(\d{6})\.(?:tar\.gz|dump)(?:\.age)?$")


def to_delete(names: Iterable[str], daily: int = 7, weekly: int = 4, monthly: int = 6) -> list[str]:
    """Names of the backups no rule keeps, oldest first; every series (dumps, file archives) separately."""
    stamps: dict[str, dt.datetime] = {}
    series: dict[str, list[str]] = {}
    for raw in names:
        if m := NAME.match(name := raw.strip()):
            stamps[name] = dt.datetime.strptime(m[2] + m[3], "%Y%m%d%H%M%S")
            series.setdefault(m[1], []).append(name)
    expired = [name for group in series.values() for name in _expired(group, stamps, daily, weekly, monthly)]
    return sorted(expired, key=lambda name: (stamps[name], name))


def _expired(group: list[str], stamps: dict[str, dt.datetime], daily: int, weekly: int, monthly: int) -> list[str]:
    """The backups of one series that no rule keeps."""
    newest_first = sorted(group, key=lambda name: stamps[name], reverse=True)
    rules: list[tuple[int, Callable[[dt.datetime], tuple]]] = [
        (daily, lambda t: (t.date(),)),
        (weekly, lambda t: tuple(t.isocalendar()[:2])),
        (monthly, lambda t: (t.year, t.month)),
    ]
    keep: set[str] = set()
    for count, period in rules:
        seen: list[tuple] = []
        for name in newest_first:  # the first backup of a period is its newest
            if period(stamps[name]) not in seen and len(seen) < count:
                seen.append(period(stamps[name]))
                keep.add(name)
    return [name for name in newest_first if name not in keep]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--daily", type=int, default=7)
    parser.add_argument("--weekly", type=int, default=4)
    parser.add_argument("--monthly", type=int, default=6)
    args = parser.parse_args()
    print("\n".join(to_delete(sys.stdin.read().split(), args.daily, args.weekly, args.monthly)))


if __name__ == "__main__":
    main()
