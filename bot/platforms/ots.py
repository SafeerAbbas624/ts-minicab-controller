"""OTS supplier portal (suppliers.ots-system.net). See SELECTORS.md section 1.
All page work is plain-data page scripts plus real clicks/keys (works in both browser modes)."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..timeutil import parse_ots_time
from .base import BidResult, BiddingPlatform, Job

log = logging.getLogger(__name__)

SEL_FILTER = 'input[name="combinedFilter"]'
SEL_ROW = "div.clickable > form.row"
SEL_FARE = 'input[name="fare"]'
REF_RX = re.compile(r"OTS-\d+-\d+-\d+")

JS_ROWS = """
const panel = document.querySelectorAll('.scroll-panel')[0];
if (!panel) return null;
return [...panel.querySelectorAll('div.clickable > form.row')].map(r => r.innerText);
"""

JS_LOADED = """
const panel = document.querySelectorAll('.scroll-panel')[0];
if (!panel) return null;
const s = [...panel.querySelectorAll('span')].find(e => /You have loaded \\d+ of \\d+/.test(e.textContent));
return s ? s.textContent : '';
"""

JS_SCROLL_LIST = """
const c = document.querySelectorAll('.scroll-container')[0];
if (c) { c.scrollTop = arguments[0] ? c.scrollHeight : 0; }
return true;
"""

JS_CLICK_ROW = """
const ref = arguments[0];
const rows = [...document.querySelectorAll('.scroll-panel')[0].querySelectorAll('div.clickable > form.row')];
const r = rows.find(x => x.innerText.split('\\n')[0].trim() === ref);
if (!r) return false;
r.parentElement.scrollIntoView({block: 'center'});
r.parentElement.click();
return true;
"""

# Finder for the visible detail panel of a ref (used inside other scripts).
_PANEL = """
const __panel = (ref) => [...document.querySelectorAll('.scroll-panel')].slice(1)
  .find(x => x.offsetParent !== null && x.innerText.includes(ref) && /Route/.test(x.innerText)) || null;
"""

JS_PANEL_INFO = _PANEL + """
const p = __panel(arguments[0]);
if (!p) return null;
const fare = p.querySelector('input[name="fare"]');
return {text: p.innerText, hasFare: !!fare, fare: fare ? fare.value : null};
"""

# Locators for click_js (return the element).
LOC_FARE = _PANEL + "const p = __panel(arguments[0]); return p ? p.querySelector('input[name=\"fare\"]') : null;"
LOC_SUBMIT = _PANEL + """
const p = __panel(arguments[0]); if (!p) return null;
const f = p.querySelector('input[name="fare"]'); if (!f) return null;
return f.closest('form').querySelector('button[type=submit]');
"""

JS_CLOSE_TAB = """
const ref = arguments[0];
const t = [...document.querySelectorAll('ul.tabs li.tab')].find(x => x.innerText.includes(ref));
if (!t) return false;
const cross = [...t.querySelectorAll('svg title')].find(x => x.textContent === 'Cross Icon');
if (!cross) return false;
const el = cross.closest('i') || cross.closest('svg');
el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
return true;
"""

JS_CAPTCHA_CHALLENGE = """
return [...document.querySelectorAll('iframe')].some(f =>
  /recaptcha/.test(f.src) && /bframe|challenge/.test(f.src + f.title) && f.offsetParent !== null
  && f.getBoundingClientRect().height > 100);
"""

MONEY = r"£\s*([\d,]+(?:\.\d{1,2})?)"


def _money(s: str) -> float:
    return float(s.replace(",", ""))


class OTSPlatform(BiddingPlatform):
    name = "ots"

    # ------------------------------------------------------------ login
    def login_state(self) -> str:
        b = self.browser
        b.tab(self.tab_name)
        return b.js("""
            const pw = [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null);
            if (pw) return 'out';
            if (document.querySelector(arguments[0])) return 'in';
            return 'loading';
        """, SEL_FILTER)

    # ------------------------------------------------------------ list
    def refresh(self) -> None:
        self.browser.reload(self.tab_name)
        self.browser.wait_for(lambda: self.login_state() != "loading", timeout=45)

    def list_jobs(self) -> list[Job]:
        b = self.browser
        b.tab(self.tab_name)
        flt = b.js("const i=document.querySelector(arguments[0]); return i ? i.value : null;", SEL_FILTER)
        if flt and flt != "Unresponded":
            log.warning("OTS filter is %r, not 'Unresponded' – reloading resets it", flt)
        b.wait_for(lambda: re.search(r"loaded \d+ of \d+", b.js(JS_LOADED) or ""), timeout=40)
        for _ in range(30):
            m = re.search(r"loaded (\d+) of (\d+)", b.js(JS_LOADED) or "")
            if not m or m.group(1) == m.group(2):
                break
            b.js(JS_SCROLL_LIST, True)
            time.sleep(1.5)
        b.js(JS_SCROLL_LIST, False)
        jobs = []
        for text in b.js(JS_ROWS) or []:
            job = self._parse_row(text)
            if job:
                jobs.append(job)
        return jobs

    def _parse_row(self, text: str) -> Job | None:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines or not REF_RX.fullmatch(lines[0]):
            return None
        job = Job(platform="OTS", ref=lines[0])
        for l in lines[1:]:
            if l.startswith("Pick up at"):
                job.pickup_text = l[len("Pick up at"):].strip()
                job.pickup = parse_ots_time(job.pickup_text)
            elif l.startswith("From "):
                m = re.match(r"From (.+?) to (.+) by (.+)$", l)
                if m:
                    job.origin, job.destination, job.vehicle = m.groups()
                else:
                    job.origin = l[5:]
            elif l.startswith("Expires on"):
                job.expires = parse_ots_time(l)
        return job

    # ------------------------------------------------------------ details
    def _panel(self, ref: str) -> dict | None:
        return self.browser.js(JS_PANEL_INFO, ref)

    def load_details(self, job: Job) -> Job:
        b = self.browser
        b.tab(self.tab_name)
        if not b.js(JS_CLICK_ROW, job.ref):
            job.extra["error"] = "row not found in list"
            return job
        info = b.wait_for(lambda: self._panel(job.ref), timeout=15, every=0.3)
        if not info:
            job.extra["error"] = "detail panel did not open"
            return job
        parse_detail_text(job, info["text"])
        job.extra["has_fare_box"] = info["hasFare"]
        job.details_loaded = True
        return job

    # ------------------------------------------------------------ bid
    def place_bid(self, job: Job, amount: float, dry_run: bool) -> BidResult:
        b = self.browser
        b.tab(self.tab_name)
        info = self._panel(job.ref)
        if not info:
            self.load_details(job)
            info = self._panel(job.ref)
        if not info:
            return BidResult(False, "detail panel not open")
        if not info["hasFare"]:
            return BidResult(False, "no price box on this job (already quoted or closed?)")
        value = f"{amount:.2f}"
        if dry_run:
            # Do not even type in dry-run: nothing on the page changes.
            return BidResult(True, f"DRY RUN – would type {value} and press Submit")

        b.show()
        if b.js(JS_CAPTCHA_CHALLENGE):
            return BidResult(False, "reCAPTCHA challenge is showing – needs a person",
                             b.screenshot(self._shots(), f"{job.ref}-captcha"))
        if not b.click_js(LOC_FARE, job.ref):
            return BidResult(False, "could not click the price box – not submitted")
        b.press("SelectAll")
        b.press("Backspace")
        b.insert_text(value)
        time.sleep(0.3)
        typed = (self._panel(job.ref) or {}).get("fare")
        try:
            typed_ok = typed is not None and abs(float(typed) - amount) < 0.001
        except ValueError:
            typed_ok = False
        if not typed_ok:
            return BidResult(False, f"price box shows {typed!r}, expected {value} – not submitted")
        before = info["text"]
        # Submit with Enter in the price box (the Submit button sits under the reCAPTCHA
        # badge in the bottom-right corner, so a mouse click can't reach it).
        b.press("Enter")
        # What OTS shows after a bid was not observed before go-live; accept any of:
        # the price box goes away, the panel text changes, or an error appears.
        def changed():
            if b.js(JS_CAPTCHA_CHALLENGE):
                return "captcha"
            p = self._panel(job.ref)
            if not p:
                return "panel closed"
            if not p["hasFare"]:
                return "price box gone"
            return "panel text changed" if p["text"] != before else None

        outcome = b.wait_for(changed, timeout=12, every=0.5)
        if not outcome and b.click_js(LOC_SUBMIT, job.ref):  # Enter did nothing: try the button
            outcome = b.wait_for(changed, timeout=12, every=0.5)
        shot = b.screenshot(self._shots(), f"{job.ref}-after-bid")
        after = (self._panel(job.ref) or {}).get("text") or ""
        new_text = after.replace(before, "").strip()[:300] if after != before else ""
        if outcome == "captcha":
            return BidResult(False, "reCAPTCHA challenge after Submit – needs a person", shot)
        if re.search(r"error|invalid|failed|unable", new_text, re.I):
            return BidResult(False, f"OTS said: {new_text}", shot)
        if not outcome:
            return BidResult(False, f"no change on the page after Enter/Submit – bid NOT sent (£{value} left typed)", shot)
        return BidResult(True, f"submitted £{value}; page: {outcome}; {new_text}", shot)

    def cleanup(self, job: Job) -> None:
        if self.rules.get("close_job_tabs", True):
            try:
                self.browser.js(JS_CLOSE_TAB, job.ref)
            except Exception:
                pass

    def _shots(self) -> Path:
        return Path(self.cfg["records"]["folder"]) / "screenshots"


def parse_detail_text(job: Job, text: str) -> Job:
    """Fill a Job from the detail panel's text (pure function, used by the self-test)."""
    job.extra["panel_text"] = text
    m = re.search(r"route is roughly\s+([\d.,]+)\s*(miles?|mi|km|kilomet\w*)", text, re.I)
    if m:
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        job.miles = round(val * 0.621371, 1) if unit.startswith("k") else val
        job.extra["distance_text"] = m.group(0)
    m = re.search(r"expecting prices between\s*" + MONEY + r"\s*and\s*" + MONEY, text, re.I)
    if m:
        job.guide_min, job.guide_max = _money(m.group(1)), _money(m.group(2))
    m = re.search(r"Pick up at ([^\n]+)", text)
    if m:
        job.pickup_text = m.group(1).strip()
        job.pickup = parse_ots_time(job.pickup_text) or job.pickup
    m = re.search(r"(\d+ Passengers?[^\n]*)\n+([^\n]+)", text)
    if m:
        job.passengers = m.group(1).strip()
        if not job.vehicle:
            job.vehicle = m.group(2).strip()
    m = re.search(r"Notes from OTS\n+(.+?)\n+(?:Important Notice|Submit Price)", text, re.S)
    if m:
        job.notes = m.group(1).strip()
    m = re.search(r"will expire on ([^\n]+)", text)
    if m:
        job.expires = parse_ots_time(m.group(1)) or job.expires
    m = re.search(r"Route\n+(.+?)\n+(.+?)\n", text)
    if m and not job.origin:
        job.origin, job.destination = m.group(1).strip(), m.group(2).strip()
    return job
