"""The public status page built from the uptime issues and maintainers' announcements (scripts/status_page.py)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("status_page", Path(__file__).resolve().parents[1] / "scripts" / "status_page.py")
status_page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status_page)  # type: ignore[union-attr]

NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
BOT = {"login": "app/github-actions"}


def alert(created: str, closed: str | None, author: dict = BOT, title: str = "Kratka Wyborcza is down") -> dict:
    return {"title": title, "author": author, "createdAt": created, "closedAt": closed,
            "state": "CLOSED" if closed else "OPEN"}


class TestStatusPage(unittest.TestCase):
    def test_outage_across_midnight_counts_on_both_days(self) -> None:
        data = status_page.summary([alert("2026-11-01T23:30:00Z", "2026-11-02T00:45:00Z")], [], NOW)
        minutes = {d["date"]: d["down_minutes"] for d in data["days"]}
        self.assertEqual((minutes["2026-11-01"], minutes["2026-11-02"], minutes["2026-11-03"]), (30, 45, 0))
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["outages"], [{"start": "2026-11-01T23:30+00:00", "end": "2026-11-02T00:45+00:00", "minutes": 75}])
        # 75 minutes out of 89.5 days (the window starts at midnight 89 days ago, today counts until noon).
        self.assertAlmostEqual(data["uptime_pct"], 100 * (1 - 75 / (89.5 * 24 * 60)), places=3)

    def test_open_alert_is_down_until_now_and_only_actions_alerts_count(self) -> None:
        alerts = [alert("2026-11-03T11:00:00Z", None),
                  alert("2026-11-02T10:00:00Z", None, author={"login": "someone"}),  # anyone can open an issue
                  alert("2026-11-02T10:00:00Z", None, title="Kratka Wyborcza is down?")]
        data = status_page.summary(alerts, [], NOW)
        self.assertEqual(data["state"], "down")
        self.assertEqual(data["outages"], [{"start": "2026-11-03T11:00+00:00", "end": None, "minutes": 60}])

    def test_announcements_show_trusted_updates_escaped(self) -> None:
        notice = {"title": "Prace <b>serwisowe</b>", "body": "W nocy\nprzerwa", "state": "OPEN",
                  "createdAt": "2026-11-03T08:00:00Z", "closedAt": None, "url": "https://github.com/x/y/issues/9",
                  "comments": [{"authorAssociation": "OWNER", "body": "Start <script>", "createdAt": "2026-11-03T09:00:00Z"},
                               {"authorAssociation": "NONE", "body": "spam", "createdAt": "2026-11-03T09:30:00Z"}]}
        old = {**notice, "title": "Stare", "state": "CLOSED", "closedAt": "2026-10-01T00:00:00Z", "comments": []}
        data = status_page.summary([], [notice, old], NOW)
        self.assertEqual(data["state"], "notice")
        self.assertEqual([u["text"] for u in data["notices"][0]["updates"]], ["Start <script>"])
        self.assertEqual(data["recent_notices"], [])  # closed over 14 days ago
        page = status_page.render(data, "https://kratka-wyborcza.example.org")
        self.assertIn("Prace &lt;b&gt;serwisowe&lt;/b&gt;", page)
        self.assertIn("Start &lt;script&gt;", page)
        self.assertNotIn("spam", page)
        self.assertIn("W nocy<br>przerwa", page)
        self.assertIn("03.11.2026 10:00", page)  # 09:00 UTC is 10:00 in Warsaw in November


if __name__ == "__main__":
    unittest.main()
