"""Offline checks with real text seen on OTS / Titan on 26 Nov 2026 (no browser)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from bot.config import load_config


def _config_path():
    # real config.yaml if present, else the public example (tests don't depend on its values)
    root = Path(__file__).resolve().parents[1]
    return root / "config.yaml" if (root / "config.yaml").exists() else root / "config.example.yaml"
from bot.email_titan import parse_body
from bot.formatter import availability_alert, booking_post, day_word, fields_from_job
from bot.platforms.base import Job
from bot.platforms.ots import OTSPlatform, parse_detail_text
from bot.rules import decide, precheck
from bot.timeutil import UK, parse_ots_time

ROW = """OTS-26112026152627-86434-1
Pick up at 10:00 am, Fri 27th Nov 2026 (GMT)
From Dover Ferry Terminal, Dover, CT17 9TF to 1 Example Road, London, SW1A 1AA by Executive Car
Expires on 20:08 pm, Thu 26th Nov 2026 (GMT)"""

BOOKING = """Job Confirmation

Your quotation of £88.00 has been accepted and OTS-18112026142241-29191-1 has been assigned to you:

OTS-18112026142241-29191-1
Route

Pick Up: 2 Sample Street, Chelmsford, CM1 1AA
Drop Off: Gatwick Airport, North Terminal

Vehicle\tSaloon Car
Starting\tMonday 23rd November 2026, 13:30pm


Total\t£88.00
"""

AVAIL = """Journey Availability Request

We would like to know if you are able to carry out OTS-20112026160540-56755-1, you recently quoted £100.00.
Note
This request will expire on Friday 20th November 2026, 19:07pm (approximately 29 minutes from now), unless"""

# Real detail panel text (26 Nov 2026); guide/expiry lines are from the panel's lower part.
DETAIL = """Quotation Requests
OTS-26112026172043-44447-1
Copy Details
Route
3 Test Lane, Horsham, RH12 1AA
Heathrow Airport, Terminal 2

This route is roughly 54 miles

Details
Pick up at 14:00 pm, Thu 24th Dec 2026 (GMT)
8 Passengers with Suitcases
8 Seater Minibus
Notes from OTS
17:22 pm, Thu 26th Nov 2026 - Only use vehicles capable of transporting 8 passengers with 8 suitcases.
Important Notice

Do not do this transfer unless you receive a Job Confirmation email.

Submit Price
This tender request will expire on 09:20 am, Fri 27th Nov 2026 (GMT).

As a guide, we are expecting prices between £95.00 and £112.00.
"""

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


def main() -> int:
    cfg = load_config(_config_path())
    rules = cfg["bidding"]["ots"]

    check("time 24h+pm", parse_ots_time("20:40 pm, Thu 26th Nov 2026 (GMT)"), datetime(2026, 11, 26, 20, 40, tzinfo=UK))
    check("time am", parse_ots_time("06:30 am, Fri 27th Nov 2026 (GMT)"), datetime(2026, 11, 27, 6, 30, tzinfo=UK))
    check("time 12 am", parse_ots_time("12:15 am, Fri 27th Nov 2026 (GMT)"), datetime(2026, 11, 27, 0, 15, tzinfo=UK))
    check("time BST", parse_ots_time("10:00 am, Fri 3rd Jul 2026 (BST)").utcoffset().total_seconds(), 3600.0)
    check("time email", parse_ots_time("Monday 23rd November 2026, 13:30pm"), datetime(2026, 11, 23, 13, 30, tzinfo=UK))
    check("time email am", parse_ots_time("Thursday 1st April 2027, 05:15am"), datetime(2027, 4, 1, 5, 15, tzinfo=UK))

    job = OTSPlatform._parse_row(None, ROW)
    check("row ref", job.ref, "OTS-26112026152627-86434-1")
    check("row route", (job.origin, job.destination, job.vehicle),
          ("Dover Ferry Terminal, Dover, CT17 9TF", "1 Example Road, London, SW1A 1AA", "Executive Car"))

    now = datetime(2026, 11, 26, 17, 20, tzinfo=UK)
    check("precheck <24h", precheck(job, rules, now), "pickup in 16.7 h (< 24 h)")
    job.pickup = datetime(2026, 11, 29, 11, 0, tzinfo=UK)
    job.miles, job.guide_min, job.guide_max = 64, 103.0, 121.0
    d, _, amount = decide(job, rules, now)
    check("bid = max - 3", (d, amount), ("bid", 118.0))
    job.miles = 81
    check("skip > 80 miles", decide(job, rules, now)[0], "skip")
    job.miles, job.guide_max = 80, None
    check("skip no guide", decide(job, rules, now)[0], "skip")

    dj = parse_detail_text(Job(platform="OTS", ref="OTS-26112026172043-44447-1"), DETAIL)
    check("detail miles/guide", (dj.miles, dj.guide_min, dj.guide_max), (54.0, 95.0, 112.0))
    check("detail pickup", dj.pickup, datetime(2026, 12, 24, 14, 0, tzinfo=UK))
    check("detail pax/vehicle", (dj.passengers, dj.vehicle), ("8 Passengers with Suitcases", "8 Seater Minibus"))
    check("detail route", (dj.origin, dj.destination), ("3 Test Lane, Horsham, RH12 1AA", "Heathrow Airport, Terminal 2"))
    check("detail expires", dj.expires, datetime(2026, 11, 27, 9, 20, tzinfo=UK))
    check("detail bid", decide(dj, rules, now)[2], 109.0)
    r2 = dict(rules, skip_vehicles=["8 Seater Minibus"])
    check("skip_vehicles", precheck(dj, r2, now), "vehicle '8 Seater Minibus' is in skip_vehicles")

    fl = parse_body("booking", BOOKING.replace("Pick Up: 2 Sample Street, Chelmsford, CM1 1AA",
                                               "Flight Landing: Gatwick Airport, North Terminal")
                                       .replace("Drop Off: Gatwick Airport, North Terminal",
                                                "Drop Off: 4 Example Close, Sevenoaks, TN13 1AA"))
    check("airport pickup ('Flight Landing:')", (fl["pickup"], fl["dropoff"]),
          ("Gatwick Airport, North Terminal", "4 Example Close, Sevenoaks, TN13 1AA"))
    tabbed = parse_body("booking", "Route\n\nPick Up: A Place, X1 1AA\n\nDrop Off: B Place, Y1 1AA\n\n"
                                   "Vehicle\t Saloon Car\n\nStarting\t Friday 20th November 2026, 22:45pm\n\nTotal\t £44.00\n")
    check("text from local copy (tabs)", (tabbed["vehicle"], tabbed["total"], tabbed["starting"]),
          ("Saloon Car", 44.0, datetime(2026, 11, 20, 22, 45, tzinfo=UK)))

    f = parse_body("booking", BOOKING)
    check("email fields", (f["pickup"], f["dropoff"], f["vehicle"], f["total"]),
          ("2 Sample Street, Chelmsford, CM1 1AA", "Gatwick Airport, North Terminal", "Saloon Car", 88.0))
    text, price = booking_post(f, 35, datetime(2026, 11, 22, 9, 0, tzinfo=UK), cfg["post"], "OTS-x")
    check("post price = OTS - 15", price, 73.0)
    print("---- post preview ----\n" + text + "\n----------------------")
    check("post header", text.splitlines()[0], "🚨 TOMORROW 23NOV AT 1:30 PM 🚨")
    check("post has NET", "£73 NET" in text, True)
    check("post miles", "ABOUT 35 MILES" in text, True)
    text2, _ = booking_post(f, None, datetime(2026, 11, 22, 9, 0, tzinfo=UK), cfg["post"], "OTS-x")
    check("no miles line when unknown", "MILES" in text2, False)

    n = datetime(2026, 11, 26, 8, 0, tzinfo=UK)
    check("day TODAY MORNING", day_word(datetime(2026, 11, 26, 9, 0, tzinfo=UK), n, cfg["post"]), "TODAY MORNING")
    check("day TODAY", day_word(datetime(2026, 11, 26, 14, 0, tzinfo=UK), n, cfg["post"]), "TODAY")
    check("day TONIGHT", day_word(datetime(2026, 11, 26, 21, 25, tzinfo=UK), n, cfg["post"]), "TONIGHT")
    check("day weekday", day_word(datetime(2026, 11, 29, 9, 0, tzinfo=UK), n, cfg["post"]), "SUNDAY")

    saved = {"pickup": "2026-11-29 11:00", "from": "Gatwick Railway Station, RH6 0RD",
             "to": "American Express Community Stadium, BN1 9BL", "vehicle": "Saloon Car", "miles": 31}
    pf = fields_from_job(saved, 51.0)
    t3, p3 = booking_post(pf, 31, datetime(2026, 11, 27, 9, 0, tzinfo=UK), cfg["post"], "OTS-y")
    check("availability post header", t3.splitlines()[0], "🚨 SUNDAY 29NOV AT 11:00 AM 🚨")
    check("availability post price", (p3, "£36 NET" in t3), (36.0, True))
    check("no post without saved job", fields_from_job({}, 51.0), None)

    a = parse_body("availability", AVAIL)
    check("availability", (a["quoted"], a["expires"]), (100.0, datetime(2026, 11, 20, 19, 7, tzinfo=UK)))
    print("---- alert preview ----\n" + availability_alert("OTS-20112026160540-56755-1", a, cfg["post"]))

    print("---- email flow / Excel lock (fake inbox + fake WhatsApp, nothing sent) ----")
    from tests.flowtest import main as flow
    total = failures + flow()
    print(f"\n{'ALL PASSED' if not total else f'{total} FAILED'}")
    return 1 if total else 0
