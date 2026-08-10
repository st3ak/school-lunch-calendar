#!/usr/bin/env python3
"""Fetch school menus from the MealViewer API and emit an iCalendar feed."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_TEMPLATE = "https://api.mealviewer.com/api/v4/school/{slug}/{start}/{end}/json"
USER_AGENT = "mealviewer-to-ics/1.0 (personal calendar sync)"
REQUEST_TIMEOUT = 30

# Order categories the way a tray is actually built, not the way the API returns them.
CATEGORY_ORDER = ["Entrees", "Breads/Grains", "Vegetables", "Fruits", "Milk"]


@dataclass
class MealDay:
    day: date
    meal: str
    categories: dict[str, list[str]] = field(default_factory=dict)

    @property
    def entrees(self) -> list[str]:
        return self.categories.get("Entrees", [])

    def summary(self, max_entrees: int) -> str:
        highlights = self.entrees[:max_entrees] or _flatten(self.categories)[:max_entrees]
        if not highlights:
            return self.meal
        return f"{self.meal}: {', '.join(highlights)}"

    def description(self) -> str:
        lines = []
        for category in _sorted_categories(self.categories):
            lines.append(category)
            lines.extend(f"  - {item}" for item in self.categories[category])
            lines.append("")
        return "\n".join(lines).strip()


def _flatten(categories: dict[str, list[str]]) -> list[str]:
    return [item for category in _sorted_categories(categories) for item in categories[category]]


def _sorted_categories(categories: dict[str, list[str]]) -> list[str]:
    known = [c for c in CATEGORY_ORDER if c in categories]
    extra = sorted(c for c in categories if c not in CATEGORY_ORDER)
    return known + extra


def fetch_range(slug: str, start: date, end: date) -> dict:
    url = API_TEMPLATE.format(
        slug=slug, start=start.strftime("%m-%d-%Y"), end=end.strftime("%m-%d-%Y")
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_days(payload: dict, meals: list[str], line_filter: str | None) -> list[MealDay]:
    wanted = {meal.casefold() for meal in meals}
    results: list[MealDay] = []

    for schedule in payload.get("menuSchedules") or []:
        raw_date = (schedule.get("dateInformation") or {}).get("dateFull")
        if not raw_date:
            continue
        day = datetime.fromisoformat(raw_date).date()

        for block in schedule.get("menuBlocks") or []:
            block_name = (block.get("blockName") or "").strip()
            if block_name.casefold() not in wanted or block.get("blackedOut"):
                continue

            categories: dict[str, list[str]] = {}
            cafeteria_lines = ((block.get("cafeteriaLineList") or {}).get("data")) or []
            for cafeteria_line in cafeteria_lines:
                line_name = cafeteria_line.get("name") or ""
                if line_filter and line_filter.casefold() not in line_name.casefold():
                    continue
                items = ((cafeteria_line.get("foodItemList") or {}).get("data")) or []
                for item in items:
                    name = (item.get("item_Name") or "").strip()
                    if not name:
                        continue
                    category = (item.get("item_Type") or "Other").strip()
                    bucket = categories.setdefault(category, [])
                    # The same item repeats across cafeteria lines; keep first occurrence only.
                    if name not in bucket:
                        bucket.append(name)

            if categories:
                results.append(MealDay(day=day, meal=block_name, categories=categories))

    return results


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold to the 75-octet limit from RFC 5545 without splitting UTF-8 sequences."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    chunks = [raw[:75]]
    remainder = raw[75:]
    while remainder:
        chunks.append(remainder[:74])
        remainder = remainder[74:]

    # A split may land mid-character; shift bytes forward until each chunk decodes.
    for index in range(len(chunks) - 1):
        while chunks[index] and (chunks[index + 1][:1] and chunks[index + 1][0] & 0xC0 == 0x80):
            chunks[index + 1] = chunks[index][-1:] + chunks[index + 1]
            chunks[index] = chunks[index][:-1]

    head = chunks[0].decode("utf-8")
    tail = "".join(f"\r\n {chunk.decode('utf-8')}" for chunk in chunks[1:])
    return head + tail


def build_ics(days: list[MealDay], slug: str, calendar_name: str, max_entrees: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//mealviewer-to-ics//{escape_text(slug)}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(calendar_name)}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]

    for entry in sorted(days, key=lambda d: (d.day, d.meal)):
        uid = f"{entry.day:%Y%m%d}-{entry.meal.casefold().replace(' ', '-')}-{slug}@mealviewer"
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{escape_text(uid)}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{entry.day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{entry.day + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{escape_text(entry.summary(max_entrees))}",
                f"DESCRIPTION:{escape_text(entry.description())}",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def collect(slug: str, weeks: int, meals: list[str], line_filter: str | None) -> list[MealDay]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    days: list[MealDay] = []

    for offset in range(weeks):
        start = monday + timedelta(weeks=offset)
        end = start + timedelta(days=6)
        try:
            payload = fetch_range(slug, start, end)
        except urllib.error.HTTPError as exc:
            print(f"warning: {start:%Y-%m-%d} request failed ({exc.code})", file=sys.stderr)
            continue
        except urllib.error.URLError as exc:
            print(f"warning: {start:%Y-%m-%d} request failed ({exc.reason})", file=sys.stderr)
            continue
        days.extend(parse_days(payload, meals, line_filter))

    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--school", default="WolfLakeElementary", help="MealViewer school slug")
    parser.add_argument("--weeks", type=int, default=6, help="weeks to fetch, starting this week")
    parser.add_argument("--meals", default="Lunch", help="comma-separated meal blocks")
    # The school also publishes a Head Start/Pre-K line; matching the main line drops it.
    parser.add_argument("--line", default="Wolf Lake", help="only include this cafeteria line")
    parser.add_argument("--max-entrees", type=int, default=2, help="entrees shown in event title")
    parser.add_argument("--calendar-name", default=None, help="calendar display name")
    parser.add_argument("--output", default="docs/lunch.ics", help="output .ics path")
    args = parser.parse_args()

    meals = [meal.strip() for meal in args.meals.split(",") if meal.strip()]
    if not meals:
        parser.error("--meals requires at least one value")

    days = collect(args.school, args.weeks, meals, args.line)
    if not days:
        print("error: no menu data returned; refusing to write an empty feed", file=sys.stderr)
        return 1

    calendar_name = args.calendar_name or f"{args.school} {'/'.join(meals)}"
    ics = build_ics(days, args.school, calendar_name, args.max_entrees)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the CRLF line endings that RFC 5545 requires.
    with open(output, "w", encoding="utf-8", newline="") as handle:
        handle.write(ics)

    print(f"wrote {len(days)} events to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
