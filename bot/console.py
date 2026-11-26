"""Readable, detailed console lines for every event (the Excel/JSONL record is unchanged).

Example:
  21:24:30  BID PLACED   OTS-26112026202309-81131-2  GBP 56.00 | Sat 30 Nov 06:40 | Orpington BR6 0HJ -> Gatwick Airport | Saloon Car | 30 mi | guide 50.00-59.00 | pickup in 81.3 h ... | OTS: price box gone
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime

from .timeutil import UK

# ANSI colours (Windows 10+ consoles understand them once VT mode is on).
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\033[{c}m" for c in (91, 92, 93, 94, 95, 96))

# event -> (label, colour)
LABELS = {
    "bot_started": ("BOT STARTED", BOLD + CYAN), "bot_stopped": ("BOT STOPPED", BOLD + YELLOW),
    "rules_saved": ("RULES", CYAN), "rules_changed": ("RULES CHANGED", BOLD + YELLOW),
    "groups_checked": ("WA GROUPS", CYAN),
    "bid_round": ("OTS LIST", DIM), "job_seen": ("NEW JOB", CYAN), "bid_skipped": ("SKIP", DIM),
    "bid_deferred": ("DEFERRED", YELLOW), "job_error": ("JOB ERROR", RED),
    "bid_placed": ("BID PLACED", BOLD + GREEN), "bid_dry_run": ("BID (DRY RUN)", GREEN),
    "bid_failed": ("BID FAILED", BOLD + RED), "bid_given_up": ("BID GIVEN UP", BOLD + RED),
    "bid_not_registered": ("BID NOT SEEN", BOLD + RED),
    "email_baseline": ("EMAIL START", CYAN), "email_round": ("EMAIL CHECK", DIM),
    "email_availability": ("EMAIL AVAIL.", BOLD + MAGENTA), "email_booking": ("EMAIL BOOKED", BOLD + MAGENTA),
    "email_reconfirmation": ("EMAIL CHANGED", BOLD + MAGENTA), "email_error": ("EMAIL ERROR", RED),
    "post_queued": ("POST QUEUED", BLUE), "post_skipped": ("POST SKIPPED", YELLOW), "post_dedup": ("POST DEDUP", DIM),
    "whatsapp_post": ("WA POSTED", GREEN), "whatsapp_post_failed": ("WA POST FAIL", RED),
    "whatsapp_alert": ("WA ALERT SENT", BOLD + GREEN), "whatsapp_alert_failed": ("WA ALERT FAIL", BOLD + RED),
    "whatsapp_dropped": ("WA DROPPED", YELLOW), "whatsapp_gave_up": ("WA GAVE UP", BOLD + RED),
    "login_required": ("LOGGED OUT", BOLD + RED), "login_ok": ("LOGIN OK", GREEN),
    "chrome_closed": ("CHROME CLOSED", BOLD + RED), "chrome_back": ("CHROME BACK", GREEN),
    "browser_lost": ("BROWSER LOST", YELLOW), "error": ("ERROR", BOLD + RED),
    "round_summary": ("ROUND DONE", BOLD),
}


def enable_colours() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # switches the Windows console to VT (ANSI) mode
    return True


def _short_place(s: str) -> str:
    s = (s or "").strip()
    return s if len(s) <= 45 else s[:44] + "…"


def _when(pickup: str) -> str:
    try:
        return datetime.strptime(pickup[:16], "%Y-%m-%d %H:%M").strftime("%a %d %b %H:%M")
    except (ValueError, TypeError):
        return pickup or ""


def _money(v) -> str:
    try:
        return f"£{float(v):.2f}"
    except (TypeError, ValueError):
        return ""


def _flat(s, n=110) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def format_event(row: dict) -> tuple[str, str]:
    """(text, colour) for one event row (same dict that goes to events.jsonl)."""
    ev = row.get("event", "")
    label, colour = LABELS.get(ev, (ev.upper().replace("_", " "), ""))
    if row.get("dry_run") == "yes" and ev.startswith(("whatsapp_", "bid_placed")):
        label += " (DRY)"
    parts: list[str] = []
    add = lambda x: x and parts.append(x)  # noqa: E731

    if ev in ("job_seen", "bid_skipped", "bid_placed", "bid_dry_run", "bid_failed", "bid_given_up",
              "bid_not_registered", "bid_deferred", "job_error"):
        if ev in ("bid_placed", "bid_dry_run", "bid_failed"):
            add(_money(row.get("bid")))
        add(_when(row.get("pickup", "")))
        if row.get("from") or row.get("to"):
            add(f"{_short_place(row.get('from'))} -> {_short_place(row.get('to'))}")
        add(row.get("vehicle"))
        add(row.get("passengers"))
        if row.get("miles") not in (None, ""):
            add(f"{float(row['miles']):g} mi")
        if row.get("guide_max") not in (None, ""):
            add(f"OTS guide {_money(row.get('guide_min'))}-{_money(row.get('guide_max'))}")
        if ev == "bid_skipped":
            parts.insert(0, f"WHY: {row.get('reason', '')}")
        elif row.get("reason") and ev != "job_seen":
            add(("rule: " if ev.startswith("bid_p") else "") + _flat(row.get("reason"), 90))
        if ev == "job_seen":
            add(_flat(row.get("details"), 40))
        elif ev in ("bid_placed", "bid_dry_run", "bid_failed"):
            d = _flat(str(row.get("details", "")).split(" | screenshot:")[0], 200)
            m = re.search(r"page: ([^;]+)", d)
            add("OTS: " + (m.group(1).strip() if m else d[:80]))
    elif ev.startswith("email_") and ev not in ("email_round", "email_baseline"):
        add(row.get("email_subject"))
        add(_when(row.get("pickup", "")))
        if row.get("from") or row.get("to"):
            add(f"{_short_place(row.get('from'))} -> {_short_place(row.get('to'))}")
        add(row.get("vehicle"))
        add(_money(row.get("bid")) and ("OTS " + _money(row.get("bid"))))
        add(_flat(row.get("reason"), 90))
        add(_flat(row.get("details"), 90))
    elif ev.startswith("whatsapp_"):
        add(f"to: {row.get('wa_target', '')}")
        add(_flat(row.get("reason"), 70))
        first = next((l.strip() for l in str(row.get("details", "")).splitlines() if l.strip()), "")
        add(f'"{_flat(first, 60)}"' if first else "")
    else:
        add(_flat(row.get("reason"), 120))
        add(_flat(row.get("details"), 160))

    ref = row.get("ref") or ""
    plat = row.get("platform") or ""
    head = f"{row.get('time_uk', '')[11:19]}  {label:<14} " + (f"{ref:<27} " if ref else (f"{plat:<27} " if plat else ""))
    return head + " | ".join(p for p in parts if p), colour


class ConsoleFormatter(logging.Formatter):
    """Coloured short lines for the console; events carry their own text/colour."""

    def __init__(self, colours: bool):
        super().__init__()
        self.colours = colours

    def format(self, record: logging.LogRecord) -> str:
        colour = getattr(record, "colour", None)
        if colour is None:  # ordinary log line from a module
            t = datetime.fromtimestamp(record.created, UK).strftime("%H:%M:%S")
            colour = RED if record.levelno >= logging.ERROR else YELLOW if record.levelno >= logging.WARNING else DIM
            text = f"{t}  {record.levelname:<14} {record.getMessage()}"
        else:
            text = record.getMessage()
        return f"{colour}{text}{RESET}" if self.colours and colour else text
