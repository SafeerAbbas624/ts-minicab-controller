"""Offline end-to-end checks of the email -> WhatsApp flow and the Excel record, with a fake
inbox and a fake WhatsApp (nothing is opened or sent). Run: python run_bot.py --selftest"""
from __future__ import annotations

import ctypes
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from bot.config import load_config


def _config_path():
    # real config.yaml if present, else the public example (tests don't depend on its values)
    root = Path(__file__).resolve().parents[1]
    return root / "config.yaml" if (root / "config.yaml").exists() else root / "config.example.yaml"
from bot.controller import Controller
from bot.email_titan import OTSEmail, parse_body, parse_list_date
from bot.timeutil import UK, now_uk

results: list[tuple[str, bool, str]] = []


def check(name, got, want):
    results.append((name, got == want, "" if got == want else f"got {got!r}, want {want!r}"))


class FakeMail:
    def __init__(self, rows, bodies):
        self.rows, self.bodies, self.opened = rows, bodies, []

    def refresh(self): pass
    def login_state(self): return "in"
    def ots_rows(self): return [dict(r) for r in self.rows]

    def read(self, row, restore_unread):
        self.opened.append(row["subject"])
        body = self.bodies[row["subject"]]
        m = OTSEmail(kind=row["kind"], ref=row["ref"], subject=row["subject"], received=row["date"], body=body)
        m.fields = parse_body(m.kind, body)
        return m


class FakeWA:
    def __init__(self): self.sent = []
    def login_state(self): return "in"
    def ensure_home(self): pass

    def post_to_group(self, name, text, dry_run):
        self.sent.append(("group", name)); return True, "sent"

    def message_number(self, phone, text, dry_run):
        self.sent.append(("alert", phone)); return True, "sent"


class FakeBrowser:
    def js(self, *a): return 0
    def wait_for(self, fn, timeout=0, every=0): return fn()


def _avail_body(ref, quoted, expires: datetime):
    return (f"Journey Availability Request\n\nWe would like to know if you are able to carry out {ref}, "
            f"you recently quoted £{quoted:.2f}.\nThis request will expire on "
            f"{expires.strftime('%A')} {expires.day}th {expires.strftime('%B %Y')}, {expires.strftime('%H:%M')}pm (approx)")


def _booking_body(ref, start: datetime, total):
    return (f"Job Confirmation\n\nYour quotation of £{total:.2f} has been accepted and {ref} has been assigned to you:\n\n"
            f"Pick Up: A Street, London, SW1A 1AA\nDrop Off: Gatwick Airport, North Terminal\n\n"
            f"Vehicle\tSaloon Car\nStarting\t{start.strftime('%A')} {start.day}th {start.strftime('%B %Y')}, "
            f"{start.strftime('%H:%M')}pm\n\n\nTotal\t£{total:.2f}\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tsm-flow-"))
    cfg = load_config(_config_path())
    cfg["records"]["folder"] = str(tmp)
    cfg["whatsapp"]["groups"] = ["G1", "G2", "G3"]
    cfg["whatsapp"]["alert_contacts"] = [{"name": "A", "phone": "447000000001"}, {"name": "B", "phone": "447000000002"}]
    import bot.controller as ctl
    ctl.desktop_alert = lambda *a, **k: None          # no popups in tests

    c = Controller(cfg, dry_run=False)
    c.browser, c.wa = FakeBrowser(), FakeWA()
    now = now_uk()
    today = date.today()
    R = c.records

    # A job the bot bid on, pickup in 3 days (details saved at bid time)
    pick = (now + timedelta(days=3)).replace(second=0, microsecond=0)
    R.update_job("OTS-1-1-1", status="bid", bid=51.0, pickup=pick.strftime("%Y-%m-%d %H:%M"),
                 **{"from": "Gatwick Railway Station, RH6 0RD"}, to="Brighton, BN1 9BL", vehicle="Saloon Car", miles=31)

    old_conf = "Job Confirmation - OTS-0-0-1"           # already read before the bot existed
    avail = "Journey Availability Request - OTS-1-1-1"   # opened by someone on a phone (read)
    rows = [
        {"unread": False, "subject": old_conf, "kind": "booking", "ref": "OTS-0-0-1", "date": "Nov 23",
         "day": today - timedelta(days=1)},
    ]
    bodies = {old_conf: _booking_body("OTS-0-0-1", now + timedelta(days=1), 88)}
    c.mail = FakeMail(rows, bodies)

    # Round 1: first run -> baseline; the old read confirmation is NOT posted
    c.email_round(); c.send_outbox()
    check("baseline: old read email not posted", c.wa.sent, [])

    # Round 2: an Availability Request arrives and someone opens it on a phone before the bot
    c.mail.rows.append({"unread": False, "subject": avail, "kind": "availability", "ref": "OTS-1-1-1",
                        "date": now.strftime("%I:%M %p"), "day": today})
    c.mail.bodies[avail] = _avail_body("OTS-1-1-1", 51, (now + timedelta(minutes=30)).astimezone(UK))
    c.email_round(); c.send_outbox()
    check("read availability email still handled: 3 groups + 2 alerts",
          sorted(c.wa.sent), sorted([("group", "G1"), ("group", "G2"), ("group", "G3"),
                                      ("alert", "447000000001"), ("alert", "447000000002")]))

    # Round 3: same inbox again -> nothing new
    n = len(c.wa.sent)
    c.email_round(); c.send_outbox()
    check("no duplicates next round", len(c.wa.sent), n)

    # Round 4: the Job Confirmation for that job -> not posted again (already posted at availability)
    conf = "Job Confirmation - OTS-1-1-1"
    c.mail.rows.append({"unread": True, "subject": conf, "kind": "booking", "ref": "OTS-1-1-1",
                        "date": now.strftime("%I:%M %p"), "day": today})
    c.mail.bodies[conf] = _booking_body("OTS-1-1-1", pick, 51)
    c.email_round(); c.send_outbox()
    check("confirmation after availability: no second post", len(c.wa.sent), n)
    check("job status booked", R.job("OTS-1-1-1")["status"], "booked")

    # Round 5: a confirmation whose pickup has already passed (bot was off) -> no post
    late = "Job Confirmation - OTS-2-2-1"
    c.mail.rows.append({"unread": True, "subject": late, "kind": "booking", "ref": "OTS-2-2-1",
                        "date": "Yesterday", "day": today - timedelta(days=1)})
    c.mail.bodies[late] = _booking_body("OTS-2-2-1", now - timedelta(hours=2), 60)
    c.email_round(); c.send_outbox()
    check("past pickup: not posted", len(c.wa.sent), n)

    # Round 6: an outbox item that waited too long is dropped, not sent
    R.state["outbox"].append({"id": "x1", "kind": "group", "target": "G1", "ref": "OTS-9-9-9", "text": "t",
                              "tries": 0, "not_after": (now - timedelta(minutes=1)).isoformat()})
    c.send_outbox()
    check("stale post dropped", (len(c.wa.sent), R.state["outbox"]), (n, []))

    # Crash safety: queueing the same job again never doubles the posts
    c.queue_posts("OTS-1-1-1", "again", not_after=pick)
    check("re-queue after crash adds nothing", R.state["outbox"], [])

    # Titan date column
    check("date '8:40 pm' = today", parse_list_date("8:40 pm", today), today)
    check("date 'Nov 25'", parse_list_date("Nov 25", date(2026, 11, 27)), date(2026, 11, 25))
    check("date 'Dec 30' in Jan = last year", parse_list_date("Dec 30", date(2027, 1, 2)), date(2026, 12, 30))
    check("date unknown", parse_list_date("whenever", today), None)

    # Excel open in another program (locked like Excel does): no crash, rows written later
    xlsx = Path(R.xlsx)
    R.flush()
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    h = k32.CreateFileW(str(xlsx), 0x80000000, 0x1, None, 3, 0x80, None)   # read, share READ only
    try:
        R.event("excel_lock_test", details="written while the file was open")
        check("Excel open: row kept waiting", len(R._pending), 1)
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))
    R.event("excel_lock_test", details="after closing")
    from openpyxl import load_workbook
    ws = load_workbook(xlsx)[now.strftime("%Y-%m-%d")]
    got = [r[3] for r in ws.iter_rows(values_only=True)].count("excel_lock_test")
    check("Excel closed: both rows written", (len(R._pending), got), (0, 2))

    # Changing the hour limit (24 h -> 12 h): a job skipped before is checked again and bid on
    from bot.platforms.base import BidResult, Job

    class FakeOTS:
        name = "ots"
        def __init__(self, rules): self.rules, self.bids = rules, []
        def refresh(self): pass
        def login_state(self): return "in"
        def list_jobs(self):
            return [Job(platform="OTS", ref="OTS-7-7-7", pickup=now + timedelta(hours=16), origin="A", destination="B",
                        vehicle="Saloon Car")]
        def load_details(self, job):
            job.miles, job.guide_min, job.guide_max = 20, 40.0, 50.0
            job.extra["has_fare_box"] = True
            return job
        def place_bid(self, job, amount, dry_run):
            self.bids.append(amount); return BidResult(True, "fake bid")
        def cleanup(self, job): pass

    c.check_rules_changed()
    p = FakeOTS(dict(cfg["bidding"]["ots"]))
    c.bid_round(p)
    check("24 h rule: job 16 h away skipped", p.bids, [])
    cfg["bidding"]["ots"]["min_hours_before_pickup"] = 12      # you edit config.yaml and restart
    c.check_rules_changed()
    p.rules = dict(cfg["bidding"]["ots"])
    c.bid_round(p)
    check("12 h rule after restart: same job now bid at £50-3", p.bids, [47.0])
    c.bid_round(p)
    check("never bid twice on the same job", p.bids, [47.0])

    shutil.rmtree(tmp, ignore_errors=True)
    for name, ok, why in results:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "\n     " + why))
    return sum(not ok for ok, in [(r[1],) for r in results])
