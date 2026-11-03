"""Retention of the nightly backups on the Storage Box (scripts/backup_retention.py)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("backup_retention", Path(__file__).resolve().parents[1] / "scripts" / "backup_retention.py")
backup_retention = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_retention)  # type: ignore[union-attr]


def name(day: dt.date, time: str = "031500") -> str:
    return f"plujka-{day:%Y%m%d}-{time}.dump"


class TestRetention(unittest.TestCase):
    def test_seven_days_four_weeks_six_months(self) -> None:
        last = dt.date(2026, 11, 1)  # a Sunday
        names = [name(last - dt.timedelta(days=n)) for n in range(400)]
        kept = set(names) - set(backup_retention.to_delete(names))
        expected = {dt.date(2026, 10, d) for d in range(26, 32)} | {last}           # the last 7 days
        expected |= {dt.date(2026, 10, 25), dt.date(2026, 10, 18), dt.date(2026, 10, 11)}  # Sundays of 3 earlier weeks
        expected |= {dt.date(2026, 9, 30), dt.date(2026, 8, 31), dt.date(2026, 7, 31), dt.date(2026, 6, 30)}  # month ends
        self.assertEqual(kept, {name(d) for d in expected})

    def test_nothing_is_deleted_when_backups_stopped(self) -> None:
        # Three old backups and none since: periods count existing backups, not calendar days.
        names = [name(dt.date(2025, 1, 5)), name(dt.date(2025, 3, 9)), name(dt.date(2025, 6, 1))]
        self.assertEqual(backup_retention.to_delete(names), [])

    def test_newest_of_a_day_and_iso_weeks_across_the_new_year(self) -> None:
        names = [name(dt.date(2026, 12, 31), "031500"), name(dt.date(2026, 12, 31), "120000"), name(dt.date(2027, 1, 3))]
        # Two days, each kept by its newest backup: the earlier backup of 31 December goes.
        self.assertEqual(backup_retention.to_delete(names, daily=2, weekly=0, monthly=0), [name(dt.date(2026, 12, 31), "031500")])
        # 31 December 2026 and 3 January 2027 are both in ISO week 2026-W53: one weekly backup, the newest.
        self.assertEqual(backup_retention.to_delete(names, daily=0, weekly=1, monthly=0),
                         [name(dt.date(2026, 12, 31), "031500"), name(dt.date(2026, 12, 31), "120000")])

    def test_dumps_and_file_archives_are_separate_series(self) -> None:
        day1, day2 = dt.date(2026, 1, 1), dt.date(2026, 1, 2)
        files = [f"plujka-files-{d:%Y%m%d}-031600.tar.gz" for d in (day1, day2)]
        # One day each: the newer dump and the newer archive stay, though the archive of day 2 is the newest of all.
        self.assertEqual(backup_retention.to_delete([name(day1), name(day2), *files], daily=1, weekly=0, monthly=0),
                         [name(day1), files[0]])

    def test_encrypted_backups_continue_their_series(self) -> None:
        # After encryption is switched on, the plain dumps of earlier days age out like any other.
        names = [name(dt.date(2026, 1, 1)), name(dt.date(2026, 1, 2)) + ".age", name(dt.date(2026, 1, 3)) + ".age"]
        self.assertEqual(backup_retention.to_delete(names, daily=2, weekly=0, monthly=0), [name(dt.date(2026, 1, 1))])

    def test_other_files_are_never_deleted(self) -> None:
        names = ["plujka-20260101-031500.dump.partial", "plujka-20260101-031500.dump.age.partial", "notes.txt", name(dt.date(2026, 1, 1)), name(dt.date(2026, 1, 2))]
        self.assertEqual(backup_retention.to_delete(names, daily=1, weekly=0, monthly=0), [name(dt.date(2026, 1, 1))])


if __name__ == "__main__":
    unittest.main()
