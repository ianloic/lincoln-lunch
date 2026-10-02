#!/usr/bin/env python3
"""Build an iCalendar feed of the Lincoln Middle School (Alameda, CA) lunch menu.

AUSD publishes menus through School Nutrition and Fitness ("webmenus2"). The
menu pages are an Angular app, but the data behind them comes from a public
GraphQL API that returns structured items (product name + category per day),
so no HTML scraping is needed.

  1. The `menuType(id)` query gives the currently published month's menu id.
  2. The `menu(id)` query returns that month's items plus links to the
     previous and next published months, which we walk in both directions.

Both hosts sit behind Cloudflare, which occasionally 403s GitHub Actions
runners, so only the API host is used and 403/429 responses are retried.

Usage: python3 lincoln_lunch.py --out site
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

MENU_TYPE_ID = "5dc0a5f0534a130f1291d943"  # "Lincoln Middle School Lunch Menu"
# The district's link to the menu, for humans.
SOURCE_URL = "https://www.schoolnutritionandfitness.com/downloadMenu.php/1571761233982/904073"
GRAPHQL_URL = "https://api.schoolnutritionandfitness.com/graphql"
VIEW_URL = "https://www.schoolnutritionandfitness.com/webmenus2/#/view?id={id}&siteCode=6467"
# The API 403s Python's default User-Agent.
USER_AGENT = "lincoln-lunch/1.0 (+https://github.com/ianloic/lincoln-lunch)"

CALENDAR_NAME = "Lincoln Middle School Lunch"
UID_DOMAIN = "lincoln-lunch.ianloic.github.io"
FOOTER = "Meals are served with seasonal fruits and vegetables and 1% or non-fat flavored milk. Menu subject to change without notice."

MENU_TYPE_QUERY = """
query ($id: String!) {
  menuType(id: $id) { name defaultPublishedMonth { id } }
}
"""

MENU_QUERY = """
query ($id: String!) {
  menu(id: $id) {
    id
    month
    year
    items { day month year hidden product { name category } }
    previousMonthPublished { id }
    nextMonthPublished { id }
  }
}
"""


@dataclass
class Menu:
    id: str
    year: int
    month: int  # 1-12 (the API's is 0-based)
    items: list
    prev_id: str | None
    next_id: str | None


@dataclass
class Dish:
    names: list  # e.g. ["Chicken Nuggets", "& Corn Bread"]
    category: str  # category of the first item, lowercased ("entrees", "sides", ...)

    def __str__(self):
        text = self.names[0]
        for name in self.names[1:]:
            text += " " if is_accompaniment(name) else ", "
            text += name
        return text


@dataclass
class Day:
    date: dt.date
    menu_id: str
    dishes: list = field(default_factory=list)

    @property
    def summary(self):
        entrees = [d.names[0] for d in self.dishes if d.category == "entrees"]
        return " / ".join(entrees or [d.names[0] for d in self.dishes])

    @property
    def description(self):
        return "\n".join([str(d) for d in self.dishes] + ["", FOOTER])


# --------------------------------------------------------------------------
# Fetching


# Cloudflare's bot protection sometimes answers 403 (or 429) to requests from
# cloud IPs; those are worth retrying like server errors.
RETRY_STATUS = {403, 408, 429}


def with_retries(fn, attempts=5, delay=5):
    """Call fn(), retrying transient network, server and Cloudflare errors with backoff."""
    for attempt in range(attempts):
        try:
            return fn()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            permanent = isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code not in RETRY_STATUS
            if permanent or attempt == attempts - 1:
                raise
            print(f"warning: {e}; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2


def graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )

    def fetch():
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    payload = with_retries(fetch)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors for {variables}: {payload['errors']}")
    return payload["data"]


def current_menu_id():
    """The id of the menu type's currently published month."""
    menu_type = graphql(MENU_TYPE_QUERY, {"id": MENU_TYPE_ID})["menuType"]
    current = (menu_type or {}).get("defaultPublishedMonth")
    if not current:
        raise RuntimeError(f"No published month for menu type {MENU_TYPE_ID}: {menu_type!r}")
    return current["id"]


def fetch_menu(menu_id):
    return parse_menu(graphql(MENU_QUERY, {"id": menu_id})["menu"])


def parse_menu(m):
    return Menu(
        id=m["id"],
        year=int(m["year"]),
        month=int(m["month"]) + 1,
        items=m.get("items") or [],
        prev_id=(m.get("previousMonthPublished") or {}).get("id"),
        next_id=(m.get("nextMonthPublished") or {}).get("id"),
    )


def fetch_menus(start_id, back, forward):
    """Fetch the starting menu plus up to `back` earlier and `forward` later months."""
    start = fetch_menu(start_id)
    menus = [start]
    seen = {start.id}
    for direction, limit in (("prev_id", back), ("next_id", forward)):
        cur = start
        for _ in range(limit):
            nxt = getattr(cur, direction)
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            cur = fetch_menu(nxt)
            menus.append(cur)
    return sorted(menus, key=lambda m: (m.year, m.month))


# --------------------------------------------------------------------------
# Interpreting menu items


def clean(name):
    return re.sub(r"\s+", " ", name or "").strip()


def is_accompaniment(name):
    """Items like "w/ French Fries", "& Naan" or "on whole wheat bun" that
    qualify the preceding item rather than being a dish of their own."""
    return bool(re.match(r"(w/|&|\+|with\b|and\b|[a-z])", name))


def starts_dish(name):
    return name.lower().startswith("salad bar")


def menu_days(menu):
    """Group a month's items into per-day lists of dishes.

    Within a day the menu lists dishes in display order, with blank (nameless)
    products used as spacers between options, e.g.

        Colonel Crunch Chicken Sandwich (Entrees)
        w/ French Fries
        <blank>
        Plant Based ChiK'n Patty (Entrees)
        w/ French Fries
        <blank>
        Salad Bar Featured Item: Fiesta Corn Salad (Sides)

    Spacers are entered by hand and are occasionally missing or misplaced, and
    categories are sometimes wrong (an "& Naan" filed under Entrees), so:
    accompaniments always attach to the preceding dish; otherwise a new dish
    starts after a spacer, at the salad bar item, or at an entree when the
    current dish is already an entree.
    """
    by_day = defaultdict(list)
    for item in menu.items:
        if item.get("hidden"):
            continue
        by_day[int(item["day"])].append(item)

    days = []
    for day_num, items in sorted(by_day.items()):
        try:
            date = dt.date(menu.year, menu.month, day_num)
        except ValueError:
            print(f"warning: menu {menu.id} has invalid day {menu.year}-{menu.month}-{day_num}", file=sys.stderr)
            continue
        day = Day(date=date, menu_id=menu.id)
        dish = None
        for item in items:
            product = item.get("product") or {}
            name = clean(product.get("name"))
            category = clean(product.get("category")).lower()
            if not name:
                dish = None
                continue
            if is_accompaniment(name) and day.dishes:
                dish = day.dishes[-1]
            elif dish is None or starts_dish(name) or (category == "entrees" and dish.category == "entrees"):
                dish = Dish([], category)
                day.dishes.append(dish)
            dish.names.append(name)
        if day.dishes:
            days.append(day)
    return days


def all_days(menus):
    out = {}
    for menu in menus:
        for day in menu_days(menu):
            if day.date in out:
                print(f"warning: {day.date} appears in more than one menu; using {menu.id}", file=sys.stderr)
            out[day.date] = day
    return [out[d] for d in sorted(out)]


# --------------------------------------------------------------------------
# iCalendar output (RFC 5545)


def ics_escape(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    """Fold a content line to at most 75 octets per physical line.

    Folds never split a UTF-8 sequence or a backslash escape: both are legal
    per RFC 5545, but not every client unfolds them correctly.
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, limit = [], 75
    while data:
        cut = min(limit, len(data))
        while cut < len(data) and (data[cut] & 0xC0) == 0x80:  # don't split UTF-8 sequences
            cut -= 1
        if cut < len(data) and (len(data[:cut]) - len(data[:cut].rstrip(b"\\"))) % 2:
            cut -= 1  # don't split "\n", "\,", etc.
        parts.append(data[:cut].decode("utf-8"))
        data = data[cut:]
        limit = 74  # continuation lines start with a space
    return "\r\n ".join(parts)


def build_ics(days, now):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ianloic//lincoln-lunch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(CALENDAR_NAME)}",
        f"NAME:{ics_escape(CALENDAR_NAME)}",
        "X-WR-CALDESC:Lunch menu for Lincoln Middle School\\, Alameda Unified School District",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for day in days:
        start = day.date.strftime("%Y%m%d")
        end = (day.date + dt.timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{start}@{UID_DOMAIN}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{ics_escape(day.summary)}",
            f"DESCRIPTION:{ics_escape(day.description)}",
            f"URL:{VIEW_URL.format(id=day.menu_id)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(ics_fold(line) + "\r\n" for line in lines)


# --------------------------------------------------------------------------
# HTML landing page


def build_html(days, now, today):
    upcoming = [d for d in days if d.date >= today]
    rows = []
    for day in upcoming:
        dishes = "".join(f"<li>{html.escape(str(d))}</li>" for d in day.dishes)
        rows.append(
            f'<section class="day"><h2>{day.date.strftime("%A, %B")} {day.date.day}</h2><ul>{dishes}</ul></section>'
        )
    if not rows:
        rows.append("<p>No upcoming menu has been published yet.</p>")
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lincoln Lunch</title>
<link rel="alternate" type="text/calendar" title="{html.escape(CALENDAR_NAME)}" href="lincoln-lunch.ics">
<style>
  :root {{ color-scheme: light dark; --fg: #1d1d1f; --bg: #fafafa; --muted: #6e6e73; --card: #fff; --accent: #0a6e4f; --on-accent: #fff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg: #f2f2f2; --bg: #161616; --muted: #a0a0a5; --card: #222; --accent: #4fc79b; --on-accent: #0b1f17; }} }}
  body {{ font: 16px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0 auto; max-width: 40rem; padding: 1rem; }}
  h1 {{ margin-bottom: .25rem; }}
  .subscribe a {{ display: inline-block; margin: .25rem .5rem .25rem 0; padding: .5rem .9rem; border-radius: .5rem; background: var(--accent); color: var(--on-accent); text-decoration: none; }}
  .day {{ background: var(--card); border-radius: .5rem; padding: .5rem 1rem; margin: .75rem 0; }}
  .day h2 {{ font-size: 1.05rem; margin: .25rem 0; }}
  .day ul {{ margin: .25rem 0; padding-left: 1.25rem; }}
  footer, .muted {{ color: var(--muted); font-size: .875rem; }}
  code {{ overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>Lincoln Middle School Lunch</h1>
<p class="muted">Alameda Unified School District &middot; updated nightly</p>
<p class="subscribe">
  <a id="webcal" href="lincoln-lunch.ics">Subscribe in Calendar</a>
  <a id="google" href="https://calendar.google.com/calendar/r/settings/addbyurl">Add to Google Calendar</a>
  <a href="lincoln-lunch.ics" download>Download .ics</a>
</p>
<p class="muted">Other calendar apps: subscribe to <code id="feed-url">lincoln-lunch.ics</code></p>
{"".join(rows)}
<footer>
<p>{html.escape(FOOTER)}</p>
<p>Source: <a href="{html.escape(SOURCE_URL)}">AUSD Food &amp; Nutrition Services</a>. Last updated {updated}.</p>
</footer>
<script>
  const feed = new URL("lincoln-lunch.ics", location.href);
  document.getElementById("feed-url").textContent = feed.href;
  const webcal = feed.href.replace(/^https?:/, "webcal:");
  document.getElementById("webcal").href = webcal;
  document.getElementById("google").href =
    "https://calendar.google.com/calendar/render?cid=" + encodeURIComponent(webcal);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("site"), help="output directory")
    p.add_argument("--menu-id", help="start from this menu id instead of the current month")
    p.add_argument("--months-back", type=int, default=12, help="earlier months to include")
    p.add_argument("--months-forward", type=int, default=6, help="later months to include")
    args = p.parse_args(argv)

    start_id = args.menu_id or current_menu_id()
    menus = fetch_menus(start_id, args.months_back, args.months_forward)
    days = all_days(menus)
    for m in menus:
        print(f"{m.year}-{m.month:02d}: menu {m.id}, {len(m.items)} items", file=sys.stderr)
    if not days:
        # Fail rather than publish an empty calendar over a good one.
        raise SystemExit("error: no menu days found")

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "lincoln-lunch.ics").write_bytes(build_ics(days, now).encode("utf-8"))
    (args.out / "index.html").write_text(build_html(days, now, today), encoding="utf-8")
    print(f"wrote {len(days)} days ({days[0].date} to {days[-1].date}) to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
