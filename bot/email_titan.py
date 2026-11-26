"""Titan webmail (hostinger.titan.email). See SELECTORS.md section 2.
Reads unread OTS emails. Never clicks any link inside an email."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .timeutil import parse_ots_time

log = logging.getLogger(__name__)

SEL_SEARCH = '.advance-thread-search-bar [contenteditable="true"]'

JS_ROWS = """
return [...document.querySelectorAll('div.list-rows div.list-item')].slice(0, arguments[0]).map((it, i) => ({
  i,
  unread: !!it.querySelector('span.unread-true'),
  sender: ((it.querySelector('.list-column-Participants') || {}).innerText || '').trim(),
  subject: ((it.querySelector('span.subject') || {}).innerText || '').trim(),
  date: ((it.querySelector('.list-column-Date') || {}).innerText || '').trim(),
}));
"""

JS_OPEN = """
const subj = arguments[0];
const it = [...document.querySelectorAll('div.list-rows div.list-item')]
  .find(x => ((x.querySelector('span.subject') || {}).innerText || '').trim() === subj);
if (!it) return false;
it.scrollIntoView({block: 'center'});
(it.querySelector('.list-column-Message') || it).click();
return true;
"""

JS_BODY = """
for (const f of document.querySelectorAll('iframe')) {
  try {
    const t = f.contentDocument && f.contentDocument.body && f.contentDocument.body.innerText;
    if (t && t.includes(arguments[0])) return t;
  } catch (e) {}
}
return null;
"""

JS_MARK_UNREAD = """
const b = document.querySelector('button.btn-unread');
if (b && b.offsetParent !== null) { b.click(); return true; }
return false;
"""


@dataclass
class OTSEmail:
    kind: str            # booking | reconfirmation | availability
    ref: str
    subject: str
    received: str
    body: str = ""
    fields: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        h = hashlib.sha1(self.body.encode("utf-8")).hexdigest()[:10] if self.body else ""
        return f"{self.subject}|{h}"


class TitanMail:
    tab_name = "email"

    def __init__(self, browser, cfg: dict):
        self.browser = browser
        self.cfg = cfg
        self.ecfg = cfg["email"]
        self.patterns = [(kind, re.compile(p))
                         for kind, key in (("booking", "booking_subjects"),
                                           ("reconfirmation", "reconfirmation_subjects"),
                                           ("availability", "availability_subjects"))
                         for p in self.ecfg.get(key, [])]

    def login_state(self) -> str:
        b = self.browser
        b.tab(self.tab_name)
        url = b.current_url()
        if "/login" in url or "/mail" not in url:
            return "out"
        return b.js("""
            if ([...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null)) return 'out';
            return document.querySelector('div.list-rows') ? 'in' : 'loading';
        """)

    def refresh(self) -> None:
        b = self.browser
        b.tab(self.tab_name)
        b._get(self.cfg["urls"]["email"])   # back to the Inbox, clears any search
        b.wait_for(lambda: self.login_state() != "loading", timeout=45)
        b.wait_for(lambda: b.js("return document.querySelectorAll('div.list-rows div.list-item').length"), timeout=20)
        if self.ecfg.get("use_search", True):
            self._search(self.ecfg["sender_contains"])

    def _search(self, text: str) -> None:
        """Type the sender into Titan's search so only OTS emails are listed
        (the inbox is busy with other senders). Falls back to the plain inbox."""
        b = self.browser
        if not b.click_js("return document.querySelector(arguments[0]);", SEL_SEARCH):
            log.warning("Titan search box not found – scanning the inbox instead")
            return
        b.press("SelectAll")
        b.press("Backspace")
        b.insert_text(text)
        b.press("Enter")
        sender = text.lower()
        ok = b.wait_for(lambda: b.js("""
            const rows = [...document.querySelectorAll('div.list-rows div.list-item')].slice(0, 5);
            return rows.length > 0 && rows.every(r => ((r.querySelector('.list-column-Participants') || {}).innerText || '')
                   .toLowerCase().includes(arguments[0]) || /Search Results/.test(document.body.innerText));
        """, sender), timeout=20)
        if not ok:
            log.warning("Titan search results did not appear – scanning whatever is listed")

    def classify(self, subject: str) -> tuple[str, str] | None:
        for kind, rx in self.patterns:
            m = rx.search(subject)
            if m:
                return kind, m.group(1)
        return None

    def ots_rows(self) -> list[dict]:
        """All listed OTS booking / availability / re-confirmation emails, read AND unread
        (someone may have opened one on a phone before the bot saw it). Each row has
        unread, subject, kind, ref, date (Titan's date column) and day (a date or None)."""
        rows = self.browser.js(JS_ROWS, self.ecfg.get("max_rows_to_scan", 60)) or []
        out = []
        for r in rows:
            if self.ecfg["sender_contains"].lower() not in r["sender"].lower():
                continue
            c = self.classify(r["subject"])
            if c:
                r["kind"], r["ref"] = c
                r["day"] = parse_list_date(r["date"])
                out.append(r)
        return out

    def read(self, row: dict, restore_unread: bool) -> OTSEmail | None:
        """Open one email and parse it. Opening marks it read; restore_unread puts it back."""
        b = self.browser
        if not b.js(JS_OPEN, row["subject"]):
            return None
        body = b.wait_for(lambda: b.js(JS_BODY, row["ref"]), timeout=20, every=0.5)
        if not body:
            log.warning("email body for %s did not load", row["subject"])
            return None
        if restore_unread:
            time.sleep(0.5)
            if not b.js(JS_MARK_UNREAD):
                log.warning("could not mark %r unread again", row["subject"])
        mail = OTSEmail(kind=row["kind"], ref=row["ref"], subject=row["subject"], received=row["date"], body=body)
        mail.fields = parse_body(mail.kind, body)
        return mail


MONEY = r"£\s*([\d,]+(?:\.\d{1,2})?)"


def parse_list_date(text: str, today: date | None = None) -> date | None:
    """Titan's date column (PC local time): '8:40 pm' = today, 'Yesterday', 'Nov 25',
    'Nov 25, 2025', '25/11/2025'. None if it can't be read."""
    today = today or date.today()
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}\s*(am|pm)?", t, re.I):
        return today
    if t.lower() in ("yesterday", "ieri"):
        return today - timedelta(days=1)
    for fmt in ("%b %d, %Y", "%d %b %Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    for fmt in ("%b %d", "%d %b"):
        try:
            d = datetime.strptime(f"{t} {today.year}", fmt + " %Y").date()
            return d if d <= today else d.replace(year=today.year - 1)
        except ValueError:
            pass
    return None


def _f(rx, text, flags=0):
    m = re.search(rx, text, flags)
    return m.group(1).strip() if m else ""


def parse_body(kind: str, body: str) -> dict:
    f: dict = {}
    if kind in ("booking", "reconfirmation"):
        f["pickup"] = _f(r"Pick Up:\s*([^\n]+)", body)
        f["dropoff"] = _f(r"Drop Off:\s*([^\n]+)", body)
        f["vehicle"] = _f(r"Vehicle\s*\t?\s*([^\n]+)", body)
        f["starting_text"] = _f(r"Starting\s*\t?\s*([^\n]+)", body)
        f["starting"] = parse_ots_time(f["starting_text"])
        total = _f(r"Total\s*\t?\s*" + MONEY, body)
        f["total"] = float(total.replace(",", "")) if total else None
        quoted = _f(r"quotation of " + MONEY, body)
        f["quoted"] = float(quoted.replace(",", "")) if quoted else None
        if kind == "reconfirmation":
            ch = _f(r"Amended Details:\s*\n(.+?)\n\s*\n\s*\n", body, re.S)
            f["changes"] = ", ".join(l.strip() for l in ch.splitlines() if l.strip())
    elif kind == "availability":
        quoted = _f(r"recently quoted " + MONEY, body)
        f["quoted"] = float(quoted.replace(",", "")) if quoted else None
        f["expires_text"] = _f(r"request will expire on ([^\n(]+)", body)
        f["expires"] = parse_ots_time(f["expires_text"])
    return f
