#!/usr/bin/env python3
"""The public status page (docs/DEPLOYMENT.md, "Status page"): a static ``index.html`` and ``status.json``.

Built from GitHub issues, so it needs nothing on the server and stays up when the server is down:

- outages: the issues "Kratka Wyborcza is down" that ``uptime.yml`` opens and closes (only those opened by GitHub
  Actions, so nobody else can fake one); open means down now;
- announcements: issues with the label ``status`` (only maintainers can label), e.g. planned maintenance;
  their comments from the owner, members or collaborators are the updates.

  gh issue list --state all --search '"Kratka Wyborcza is down" in:title' --json number,title,state,createdAt,closedAt,author > alerts.json
  gh issue list --state all --label status --json number,title,body,state,createdAt,closedAt,url,comments > notices.json
  python3 scripts/status_page.py --alerts alerts.json --notices notices.json --out site
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ALERT_TITLE = "Kratka Wyborcza is down"
ALERT_AUTHORS = {"app/github-actions", "github-actions", "github-actions[bot]"}
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
DAYS = 90
WARSAW = ZoneInfo("Europe/Warsaw")  # times on the page; the data stays in UTC


def _time(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def outages(alerts: list[dict[str, Any]], now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime | None]]:
    """(start, end) of each outage, newest first; end is None while it lasts."""
    found = []
    for issue in alerts:
        if issue.get("title") != ALERT_TITLE or (issue.get("author") or {}).get("login") not in ALERT_AUTHORS:
            continue
        start = _time(issue["createdAt"])
        assert start is not None
        found.append((start, _time(issue.get("closedAt")) if issue.get("state") == "CLOSED" else None))
    return sorted(found, key=lambda o: o[0], reverse=True)


def daily_downtime(spans: list[tuple[dt.datetime, dt.datetime | None]], now: dt.datetime) -> list[tuple[dt.date, float]]:
    """Minutes of outage on each of the last ``DAYS`` UTC days, oldest first."""
    today = now.date()
    days = []
    for back in range(DAYS - 1, -1, -1):
        day = today - dt.timedelta(days=back)
        begin = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
        end = min(begin + dt.timedelta(days=1), now)
        minutes = sum(max(0.0, (min(e or now, end) - max(s, begin)).total_seconds() / 60) for s, e in spans)
        days.append((day, minutes))
    return days


def summary(alerts: list[dict[str, Any]], notices: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    """Everything the page shows, as plain data (also written as status.json)."""
    spans = outages(alerts, now)
    days = daily_downtime(spans, now)
    period = (now - dt.datetime.combine(days[0][0], dt.time(), dt.timezone.utc)).total_seconds() / 60
    down_minutes = sum(m for _, m in days)
    open_notices = [n for n in notices if n.get("state") == "OPEN"]
    recent_notices = [n for n in notices if n.get("state") == "CLOSED" and (c := _time(n.get("closedAt")))
                      and now - c <= dt.timedelta(days=14)]
    state = "down" if spans and spans[0][1] is None else "notice" if open_notices else "ok"

    def notice(issue: dict[str, Any]) -> dict[str, Any]:
        updates = [{"at": c["createdAt"], "text": c["body"]} for c in issue.get("comments") or []
                   if c.get("authorAssociation") in TRUSTED]
        return {"title": issue["title"], "text": issue.get("body") or "", "at": issue["createdAt"],
                "closed_at": issue.get("closedAt"), "url": issue.get("url"), "updates": updates}

    return {
        "state": state,
        "updated_at": now.isoformat(timespec="seconds"),
        "uptime_pct": round(100 * (1 - down_minutes / period), 3) if period else 100.0,
        "days": [{"date": d.isoformat(), "down_minutes": round(m)} for d, m in days],
        "outages": [{"start": s.isoformat(timespec="minutes"), "end": e.isoformat(timespec="minutes") if e else None,
                     "minutes": round(((e or now) - s).total_seconds() / 60)}
                    for s, e in spans if (e or now) >= now - dt.timedelta(days=DAYS)],
        "notices": [notice(n) for n in open_notices],
        "recent_notices": [notice(n) for n in recent_notices],
    }


def _local(value: str) -> str:
    moment = _time(value)
    assert moment is not None
    return moment.astimezone(WARSAW).strftime("%d.%m.%Y %H:%M")


def _text(value: str) -> str:
    return html.escape(value).replace("\n", "<br>")


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if hours else f"{rest} min"


def render(data: dict[str, Any], site_url: str) -> str:
    """The page in Polish; every text from an issue is escaped."""
    headline = {"ok": ("Wszystko działa", "ok"), "notice": ("Komunikat dla użytkowników", "notice"),
                "down": ("Kratka Wyborcza nie odpowiada", "down")}[data["state"]]
    bars = "".join(
        f'<span class="bar {"down" if d["down_minutes"] >= 30 else "part" if d["down_minutes"] else "ok"}" '
        f'title="{d["date"]}: {_duration(d["down_minutes"]) + " przerwy" if d["down_minutes"] else "bez przerw"}"></span>'
        for d in data["days"])

    def notices(items: list[dict[str, Any]], closed: bool) -> str:
        out = []
        for n in items:
            updates = "".join(f'<li><time>{_local(u["at"])}</time> {_text(u["text"])}</li>' for u in n["updates"])
            when = f'{_local(n["at"])}' + (f' – {_local(n["closed_at"])}' if closed and n["closed_at"] else "")
            out.append(f'<article class="notice{" closed" if closed else ""}"><h3>{_text(n["title"])}</h3>'
                       f'<time>{when}</time><p>{_text(n["text"])}</p>'
                       + (f"<ul>{updates}</ul>" if updates else "") + "</article>")
        return "".join(out)

    rows = "".join(
        f'<tr><td>{_local(o["start"])}</td><td>{_local(o["end"]) if o["end"] else "trwa"}</td>'
        f'<td>{_duration(o["minutes"])}</td></tr>' for o in data["outages"])
    return f"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Status — Kratka Wyborcza</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.2rem; }}
  .state {{ padding: 1rem 1.2rem; border-radius: 12px; font-weight: 600; font-size: 1.15rem; margin: 1rem 0; }}
  .state-ok {{ background: #dcfce7; color: #14532d; }}
  .state-notice {{ background: #fef3c7; color: #78350f; }}
  .state-down {{ background: #fee2e2; color: #7f1d1d; }}
  .bars {{ display: flex; gap: 2px; height: 34px; }}
  .bar {{ flex: 1; border-radius: 2px; background: #22c55e; }}
  .bar.part {{ background: #f59e0b; }}
  .bar.down {{ background: #ef4444; }}
  .legend {{ display: flex; justify-content: space-between; color: #6b7280; font-size: 0.85rem; margin-top: 0.3rem; }}
  .notice {{ border-left: 4px solid #f59e0b; padding: 0.2rem 1rem; margin: 1rem 0; }}
  .notice.closed {{ border-color: #9ca3af; color: #4b5563; }}
  .notice h3 {{ margin: 0.4rem 0 0.1rem; font-size: 1.05rem; }}
  time {{ color: #6b7280; font-size: 0.85rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.95rem; }}
  td, th {{ text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #e5e7eb; }}
  footer {{ color: #6b7280; font-size: 0.85rem; margin-top: 2rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #111827; color: #e5e7eb; }}
    .state-ok {{ background: #14532d; color: #dcfce7; }}
    .state-notice {{ background: #78350f; color: #fef3c7; }}
    .state-down {{ background: #7f1d1d; color: #fee2e2; }}
    td, th {{ border-color: #374151; }}
  }}
</style>
</head>
<body>
<h1>Kratka Wyborcza — status usługi</h1>
<a href="{html.escape(site_url)}">{html.escape(site_url)}</a>
<div class="state state-{headline[1]}">{headline[0]}</div>
{notices(data["notices"], closed=False)}
<h2>Ostatnie {DAYS} dni</h2>
<div class="bars" role="img" aria-label="Dostępność w ostatnich {DAYS} dniach: {data["uptime_pct"]:.2f}%">{bars}</div>
<div class="legend"><span>{DAYS} dni temu</span><span>dostępność {data["uptime_pct"]:.2f}%</span><span>dziś</span></div>
<h2>Przerwy</h2>
{f"<table><tr><th>Od</th><th>Do</th><th>Czas</th></tr>{rows}</table>" if rows else "<p>Bez przerw w tym okresie.</p>"}
{"<h2>Zakończone komunikaty</h2>" + notices(data["recent_notices"], closed=True) if data["recent_notices"] else ""}
<footer>Sprawdzane co 10 minut spoza serwera. Czas polski. Aktualizacja: {_local(data["updated_at"])}.</footer>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alerts", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--site-url", default="", help="the monitored site, linked from the page")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    data = summary(json.loads(args.alerts.read_text()), json.loads(args.notices.read_text()), now)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "status.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    (args.out / "index.html").write_text(render(data, args.site_url))
    print(f"status page: {data['state']}, uptime {data['uptime_pct']}% over {DAYS} days, {len(data['outages'])} outages")


if __name__ == "__main__":
    main()
