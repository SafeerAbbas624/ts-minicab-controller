"""UK time helpers. OTS writes times like '20:40 pm, Thu 26th Nov 2026 (GMT)' or
'Monday 23rd November 2026, 13:30pm'. The PC clock is not UK time, so everything is
converted to Europe/London explicitly."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ZONES = {"GMT": timedelta(0), "UTC": timedelta(0), "BST": timedelta(hours=1)}


def now_uk() -> datetime:
    return datetime.now(UK)


def _hour(h: int, ampm: str | None) -> int:
    # OTS mixes 24h with am/pm ("20:40 pm"). Only apply am/pm to 1..12.
    if ampm and 1 <= h <= 12:
        ampm = ampm.lower()
        if ampm == "pm" and h != 12:
            return h + 12
        if ampm == "am" and h == 12:
            return 0
    return h


# '10:00 am, Fri 27th Nov 2026 (GMT)'
_OTS_PORTAL = re.compile(
    r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*,\s*[A-Za-z]{3,9}\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})"
    r"(?:\s*\(([A-Z]{3,4})\))?", re.I)
# 'Monday 23rd November 2026, 13:30pm'  /  'Friday 20th November 2026, 19:07pm'
_OTS_EMAIL = re.compile(
    r"[A-Za-z]{3,9}\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*(am|pm)?", re.I)


def _build(y, mon, d, h, mi, zone=None) -> datetime:
    month = _MONTHS[mon[:3].lower()]
    naive = datetime(int(y), month, int(d), h, int(mi))
    if zone and zone.upper() in _ZONES:
        return naive.replace(tzinfo=timezone(_ZONES[zone.upper()])).astimezone(UK)
    return naive.replace(tzinfo=UK)


def parse_ots_time(text: str) -> datetime | None:
    """Parse either OTS format; returns an aware Europe/London datetime or None."""
    if not text:
        return None
    m = _OTS_PORTAL.search(text)
    if m:
        h, mi, ap, d, mon, y, zone = m.groups()
        return _build(y, mon, d, _hour(int(h), ap), mi, zone)
    m = _OTS_EMAIL.search(text)
    if m:
        d, mon, y, h, mi, ap = m.groups()
        return _build(y, mon, d, _hour(int(h), ap), mi)
    return None


def fmt_uk(dt: datetime | None) -> str:
    return dt.astimezone(UK).strftime("%Y-%m-%d %H:%M") if dt else ""
