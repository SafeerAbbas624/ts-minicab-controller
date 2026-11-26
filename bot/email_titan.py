"""Titan webmail (hostinger.titan.email). See SELECTORS.md section 2.
Reads OTS emails from Titan's local mailbox copy. Never clicks any link inside an email."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .timeutil import UK, parse_ots_time

log = logging.getLogger(__name__)

SEL_SEARCH = '.advance-thread-search-bar [contenteditable="true"]'

# ---------------------------------------------------------------- Titan's local mailbox copy
# Titan keeps a synced copy of the mailbox in the browser (IndexedDB "Titan-<account id>":
# stores Message, MessageBody, Category). Reading it does not depend on what the page draws,
# so it also works while the Chrome window is covered / hidden.
JS_IDB_LIST = r"""
const sender = arguments[0];
const dbName = (await indexedDB.databases()).map(d => d.name).find(n => /^Titan-\d+$/.test(n || ''));
if (!dbName) return {ok: false, why: 'no Titan mailbox database in this browser'};
const db = await new Promise((res, rej) => { const r = indexedDB.open(dbName); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
try {
  const get = (s, m, k) => new Promise((res, rej) => { const q = db.transaction(s, 'readonly').objectStore(s)[m](k);
                                                      q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const cats = await get('Category', 'getAll');
  const nm = c => String(c.name || c.display_name || '').toLowerCase();
  const skip = new Set(cats.filter(c => /^(spam|trash|sent|drafts|scheduled)$/.test(nm(c))).map(c => String(c.id)));
  const inbox = cats.find(c => nm(c) === 'inbox');
  const bodies = new Set(await get('MessageBody', 'getAllKeys'));
  const msgs = await get('Message', 'getAll');
  const rows = msgs.filter(m => !skip.has(String(m.folder_id)) && JSON.stringify(m.from || '').toLowerCase().includes(sender))
    .map(m => ({id: m.id, subject: m.subject || '', date_ms: m.date, unread: !!m.unread,
                folder: String(m.folder_id), hasBody: bodies.has(m.id)}));
  return {ok: true, db: dbName, total: msgs.length, rows,
          sync: inbox && inbox.sync_progress ? inbox.sync_progress.status : '',
          newest: msgs.reduce((a, m) => Math.max(a, m.date || 0), 0)};
} finally { db.close(); }
"""

# Email text from the local copy, turned into lines like the page shows them
# (table cells separated by a tab), or null if Titan has not downloaded it yet.
JS_IDB_BODY = r"""
const id = arguments[0];
const dbName = (await indexedDB.databases()).map(d => d.name).find(n => /^Titan-\d+$/.test(n || ''));
if (!dbName) return null;
const db = await new Promise((res, rej) => { const r = indexedDB.open(dbName); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
let v;
try {
  v = await new Promise((res, rej) => { const q = db.transaction('MessageBody', 'readonly').objectStore('MessageBody').get(id);
                                        q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
} finally { db.close(); }
if (!v || !v.value) return null;
const doc = new DOMParser().parseFromString(String(v.value), 'text/html');
doc.querySelectorAll('style,script,head,title').forEach(e => e.remove());
const BLOCK = /^(P|DIV|TR|TABLE|TBODY|H[1-6]|LI|UL|OL|SECTION|CENTER|HEADER|FOOTER|BLOCKQUOTE)$/;
const walk = n => {
  if (n.nodeType === 3) return n.textContent.replace(/[ \t\r\n ]+/g, ' ');
  if (n.nodeType !== 1) return '';
  if (n.tagName === 'BR') return '\n';
  let s = ''; for (const c of n.childNodes) s += walk(c);
  if (n.tagName === 'TD' || n.tagName === 'TH') return s.trim() + '\t';
  return BLOCK.test(n.tagName) ? '\n' + s.trim() + '\n' : s;
};
return walk(doc.body).split('\n').map(l => l.replace(/\t+$/, '').trim()).join('\n').replace(/\n{3,}/g, '\n\n');
"""

# ---------------------------------------------------------------- page (only to open an email)
JS_OPEN = """
const subj = arguments[0];
const it = [...document.querySelectorAll('div.list-rows div.list-item')]
  .find(x => ((x.querySelector('span.subject') || {}).innerText || '').trim() === subj);
if (!it) return false;
it.scrollIntoView({block: 'center'});
(it.querySelector('.list-column-Message') || it).click();
return true;
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
    """Finds OTS emails in Titan's local mailbox copy (works with the window hidden) and
    reads their text from it. Only an email Titan has not downloaded yet is opened in the
    page (newest emails are at the top of the inbox); if its row is not on screen, the
    search box is used with the window brought to the front."""
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
        self.source_info = ""

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
        """Reload the inbox: Titan syncs new mail into its local copy on load."""
        b = self.browser
        b.tab(self.tab_name)
        b._get(self.cfg["urls"]["email"])
        b.wait_for(lambda: self.login_state() != "loading", timeout=45)
        b.wait_for(lambda: b.js("return document.querySelectorAll('div.list-rows div.list-item').length"), timeout=20)
        time.sleep(2)  # let the background sync write the newest mail

    def classify(self, subject: str) -> tuple[str, str] | None:
        for kind, rx in self.patterns:
            m = rx.search(subject)
            if m:
                return kind, m.group(1)
        return None

    def ots_rows(self) -> list[dict]:
        """All OTS booking / availability / re-confirmation emails in the mailbox (not spam,
        trash, sent or drafts), read AND unread. Each row: id, subject, kind, ref, unread,
        date (UK text), day (UK date), hasBody."""
        res = self.browser.js(JS_IDB_LIST, self.ecfg["sender_contains"].lower()) or {}
        if not res.get("ok"):
            self.source_info = f"LOCAL COPY NOT AVAILABLE ({res.get('why', 'unknown')})"
            log.warning("Titan local mailbox not readable: %s", res.get("why"))
            return []
        newest = datetime.fromtimestamp(res["newest"] / 1000, UK).strftime("%d %b %H:%M") if res.get("newest") else "?"
        self.source_info = (f"{res['total']} emails in Titan's local copy (sync {res.get('sync') or '?'}, "
                            f"newest {newest} UK)")
        out = []
        for r in res["rows"]:
            c = self.classify(r["subject"])
            if not c:
                continue
            when = datetime.fromtimestamp((r["date_ms"] or 0) / 1000, UK)
            r.update(kind=c[0], ref=c[1], day=when.date(), date=when.strftime("%Y-%m-%d %H:%M"))
            out.append(r)
        out.sort(key=lambda r: r["date_ms"] or 0, reverse=True)
        return out

    def _open_in_page(self, row: dict) -> bool:
        """Open one email so Titan downloads it. Newest emails are in the plain inbox list;
        otherwise search for the subject with the window in front."""
        b = self.browser
        b.tab(self.tab_name)
        if b.js(JS_OPEN, row["subject"]):
            return True
        log.info("email %r is not in the visible inbox list – searching for it", row["subject"])
        b.show()
        if b.click_js("return document.querySelector(arguments[0]);", SEL_SEARCH):
            b.press("SelectAll")
            b.press("Backspace")
            b.insert_text(row["subject"])
            b.press("Enter")
            return bool(b.wait_for(lambda: b.js(JS_OPEN, row["subject"]), timeout=20, every=1))
        return False

    def read(self, row: dict, restore_unread: bool) -> OTSEmail | None:
        """Text of one OTS email: from the local copy, opening it first if Titan has not
        downloaded it yet (opening marks it read; restore_unread puts it back)."""
        b = self.browser
        body = b.js(JS_IDB_BODY, row["id"]) if row.get("hasBody") else None
        opened = False
        if not body:
            if not self._open_in_page(row):
                log.warning("could not open %r", row["subject"])
                return None
            opened = True
            body = b.wait_for(lambda: b.js(JS_IDB_BODY, row["id"]), timeout=25, every=1)
        if not body or row["ref"] not in body:
            log.warning("email text for %r did not load", row["subject"])
            return None
        if opened and restore_unread:
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
        # Airport pickups say "Flight Landing: Gatwick Airport, North Terminal" instead of
        # "Pick Up:"; otherwise take the first "Label: place" line under "Route".
        f["pickup"] = _f(r"(?:Pick Up|Pickup|Flight Landing|Collection|Train Arrival|Ship Arrival)\s*:\s*([^\n\t]+)",
                         body, re.I)
        if not f["pickup"]:
            m = re.search(r"Route\s*\n+\s*([A-Za-z][A-Za-z ]{2,30}):\s*([^\n\t]+)", body)
            if m and "drop" not in m.group(1).lower():
                f["pickup"] = m.group(2).strip()
        f["pickup_label"] = (re.search(r"Route\s*\n+\s*([A-Za-z][A-Za-z ]{2,30}):", body) or [None, ""])[1]
        f["dropoff"] = _f(r"Drop Off:\s*([^\n\t]+)", body)
        f["vehicle"] = _f(r"Vehicle\s*\t?\s*([^\n\t]+)", body)
        f["starting_text"] = _f(r"Starting\s*\t?\s*([^\n\t]+)", body)
        f["starting"] = parse_ots_time(f["starting_text"])
        total = _f(r"Total\s*\t?\s*" + MONEY, body)
        f["total"] = float(total.replace(",", "")) if total else None
        quoted = _f(r"quotation of " + MONEY, body)
        f["quoted"] = float(quoted.replace(",", "")) if quoted else None
        if kind == "reconfirmation":
            ch = _f(r"Amended Details:\s*(.+?)\n\s*OTS-\d", body, re.S)
            f["changes"] = ", ".join(l.strip() for l in ch.splitlines() if l.strip())
    elif kind == "availability":
        quoted = _f(r"recently quoted " + MONEY, body)
        f["quoted"] = float(quoted.replace(",", "")) if quoted else None
        f["expires_text"] = _f(r"request will expire on ([^\n(]+)", body)
        f["expires"] = parse_ots_time(f["expires_text"])
    return f
