"""The main loop: check logins -> bid on each platform -> read OTS emails ->
post / alert on WhatsApp -> write records -> wait -> repeat."""
from __future__ import annotations

import json
import logging
import re
import time
import traceback
import uuid
from datetime import date, datetime
from pathlib import Path

from . import formatter
from .browser import BaseBrowser, BrowserLost, desktop_alert, make_browser
from .email_titan import TitanMail
from .platforms import PLATFORMS
from .records import Records
from .rules import decide, precheck
from .timeutil import fmt_uk, now_uk
from .whatsapp import WhatsApp

log = logging.getLogger(__name__)

# Skip reasons that cannot change for the same job while the rules stay the same: don't
# reopen the job every round. (Cleared automatically when the bidding rules change.)
PERMANENT_SKIPS = ("miles (>", "could not read pickup", "bid £", "skip_vehicles")


def _reason_key(reason: str) -> str:
    return re.sub(r"[\d.]+", "#", reason)


class Controller:
    def __init__(self, cfg: dict, dry_run: bool, only: str | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.only = only
        rc = cfg["records"]
        wb, st = rc["workbook"], rc["state_file"]
        if dry_run:  # dry-run keeps its own memory and workbook, so it never blocks a real bid/post
            wb, st = wb.replace(".xlsx", "_DRYRUN.xlsx"), st.replace(".json", ".dryrun.json")
        self.records = Records(rc["folder"], wb, st, dry_run)
        self.records.state.setdefault("outbox", [])
        self.browser: BaseBrowser | None = None
        self.platforms = []
        self.mail = self.wa = None

    # ------------------------------------------------------------ startup
    def start(self) -> None:
        self.browser = make_browser(self.cfg)
        enabled = [n for n in PLATFORMS if self.cfg["bidding"].get(n, {}).get("enabled")]
        self.browser.open_tabs(enabled + ["whatsapp", "email"])
        self.platforms = [PLATFORMS[n](self.browser, self.cfg, self.records) for n in enabled]
        self.mail = TitanMail(self.browser, self.cfg)
        self.wa = WhatsApp(self.browser, self.cfg)
        self.records.event("bot_started", details=f"platforms={[p.name for p in self.platforms]} "
                                                  f"dry_run={self.dry_run}")
        self.check_rules_changed()
        self.ensure_logged_in()
        self.check_groups()

    def check_rules_changed(self) -> None:
        """If the bidding rules in config.yaml changed since the last run (e.g. 24 h -> 12 h,
        80 -> 100 miles), forget the old 'skip for good' marks so every job is checked again
        under the new rules. Jobs already bid on are never bid on again."""
        R = self.records
        snap = json.dumps(self.cfg["bidding"], sort_keys=True)
        if R.state.get("rules") == snap:
            return
        cleared = 0
        for j in R.state["jobs"].values():
            if j.pop("skip_permanent", None):
                cleared += 1
        R.state["logged"] = {k: v for k, v in R.state["logged"].items() if not k.startswith("skip|")}
        old = R.state.get("rules")
        R.state["rules"] = snap
        R.save_state()
        R.event("rules_changed" if old else "rules_saved",
                details=f"bidding rules now: {snap}; {cleared} skipped jobs will be checked again")

    def check_groups(self) -> None:
        """Once at start: make sure every configured group is in the chat list."""
        missing = [g for g in self.cfg["whatsapp"]["groups"] if not self.wa.find_chat(g, click=False)]
        self.records.event("groups_checked", details=f"{len(self.cfg['whatsapp']['groups']) - len(missing)} found"
                                                     + (f"; NOT found: {missing}" if missing else ""))
        if missing:
            desktop_alert("TS Minicab bot – WhatsApp groups",
                          "These groups from config.yaml are not in WhatsApp:\n" + "\n".join(missing))

    def _sites(self):
        sites = [(p.name.upper(), p.login_state) for p in self.platforms]
        sites += [("WhatsApp", self.wa.login_state), ("Email", self.mail.login_state)]
        return sites

    def ensure_logged_in(self, wait_minutes: float = 10) -> None:
        """At start-up: give each site time to load; if one is logged out, ask (popup +
        console) and wait up to `wait_minutes`. After that the bot carries on anyway:
        rounds skip a logged-out site and alert you, instead of stopping everything."""
        b = self.browser
        for name, check in self._sites():
            state = b.wait_for(lambda: check() != "loading" and check(), timeout=90, every=1)
            if state == "in":
                continue
            msg = (f"{name} is not logged in in the bot's Chrome window.\n"
                   f"Please log in there now (do not close the window).")
            self.records.event("login_required", platform=name, status=state or "unknown", details=msg)
            print("\n*** " + msg.replace("\n", "\n*** ") + "\n")
            desktop_alert("TS Minicab - login needed", msg)
            end = time.time() + wait_minutes * 60
            while time.time() < end:
                time.sleep(15)
                try:
                    if check() == "in":
                        self.records.event("login_ok", platform=name)
                        break
                except BrowserLost:
                    raise
                except Exception:
                    pass

    def site_ok(self, name: str, check) -> bool:
        """Per round: True if the site is logged in. If not, log + alert ONCE per outage
        (WhatsApp to the alert numbers if WhatsApp itself works, and a PC popup)."""
        R = self.records
        down = R.state.setdefault("down", {})
        state = self.browser.wait_for(lambda: check() != "loading" and check(), timeout=60, every=1)
        if state == "in":
            if name in down:
                R.event("login_ok", platform=name, details=f"was logged out since {down.pop(name)}")
                R.save_state()
            return True
        if name not in down:
            down[name] = now_uk().strftime("%Y-%m-%d %H:%M")
            R.save_state()
            msg = (f"TS Minicab: {name} is logged out on the controller PC (since {down[name]} UK). "
                   f"Please log in again in its Chrome window. Other parts keep running.")
            R.event("login_required", platform=name, status=state or "unknown", details=msg)
            desktop_alert("TS Minicab - login needed", msg)
            if name != "WhatsApp":
                self.queue_alert("", msg, key=f"down|{name}|{down[name]}")
        return False

    # ------------------------------------------------------------ loop
    def run_forever(self, once: bool = False, minutes: float | None = None) -> None:
        interval = self.cfg.get("loop_interval_seconds", 120)
        self.start()
        stop_at = time.time() + minutes * 60 if minutes else None
        while True:
            started = time.time()
            try:
                self.run_round()
            except BrowserLost as e:
                self.records.event("browser_lost", details=str(e).splitlines()[0])
                self._restart_browser()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                shot = ""
                if self.browser and self.cfg["records"].get("screenshot_errors", True):
                    shot = self.browser.screenshot(Path(self.cfg["records"]["folder"]) / "screenshots", "error")
                self.records.event("error", reason=f"{type(e).__name__}: {e}"[:300],
                                   details=traceback.format_exc()[-1500:] + (f"\nscreenshot: {shot}" if shot else ""))
            self.records.flush()
            if once:
                return
            if stop_at and time.time() >= stop_at:
                self.records.event("bot_stopped", details=f"time limit of {minutes:g} min reached")
                return
            wait = max(5, interval - (time.time() - started))
            if stop_at:
                wait = min(wait, max(0, stop_at - time.time()))
            log.info("round done – next in %.0f s", wait)
            time.sleep(wait)

    def _restart_browser(self) -> None:
        if self.browser:
            self.browser.quit()
            if hasattr(self.browser, "bridge"):
                # keep the same bridge server; just wait for the extension and re-find the tabs
                from .bridge import wait_for_extension
                bridge = self.browser.bridge
                if not bridge.wait_connected(20):
                    # Chrome itself was closed: nothing can run (not even WhatsApp alerts).
                    self.records.event("chrome_closed", reason="Chrome / the bridge extension is gone – "
                                                               "waiting for Chrome to be opened again")
                    self.records.flush()
                    desktop_alert("TS Minicab – Chrome closed",
                                  "Chrome was closed, so the controller is paused (no bids, no posts).\n"
                                  "Open Chrome again with the bot's profile – it carries on by itself.")
                    t0 = time.time()
                    wait_for_extension(bridge)
                    self.records.event("chrome_back", details=f"Chrome back after {(time.time() - t0) / 60:.0f} min")
                time.sleep(5)
                enabled = [p.name for p in self.platforms]
                self.browser.tabs.clear()
                self.browser.open_tabs(enabled + ["whatsapp", "email"])
                self.ensure_logged_in()
                return
        time.sleep(5)
        self.start()

    def run_round(self) -> None:
        if self.only in (None, "bid"):
            for p in self.platforms:
                self.bid_round(p)
        if self.only in (None, "email"):
            self.email_round()
        self.send_outbox()

    # ------------------------------------------------------------ bidding
    def bid_round(self, p) -> None:
        R = self.records
        p.refresh()
        if not self.site_ok(p.name.upper(), p.login_state):
            return
        jobs = p.list_jobs()
        now = now_uk()
        rules = p.rules
        R.event("bid_round", platform=p.name.upper(), details=f"{len(jobs)} jobs in the Unresponded list")
        bids = 0
        for job in jobs:
            st = R.job(job.ref)
            base = dict(platform=p.name.upper(), ref=job.ref, pickup=fmt_uk(job.pickup) or job.pickup_text,
                        **{"from": job.origin, "to": job.destination}, vehicle=job.vehicle)
            if R.once(f"seen|{job.ref}"):
                R.event("job_seen", **base, details=f"expires {fmt_uk(job.expires)}")
                R.update_job(job.ref, platform=p.name.upper(), pickup=base["pickup"], **{"from": job.origin},
                             to=job.destination, vehicle=job.vehicle)
            if st.get("status") in ("bid", "availability_requested", "booked", "reconfirm_requested"):
                if not self.dry_run and st.get("status") == "bid" and R.once(f"still|{job.ref}"):
                    R.event("bid_not_registered", **base, bid=st.get("bid"),
                            reason="job is still in 'Unresponded' after our bid – check OTS by hand")
                continue
            if st.get("skip_permanent") or st.get("gave_up"):
                continue
            reason = precheck(job, rules, now)
            if reason:
                self._skip(job, base, reason)
                continue
            if bids >= rules.get("max_bids_per_round", 30):
                if R.once(f"defer|{job.ref}|{now:%Y%m%d%H}"):
                    R.event("bid_deferred", **base, reason="max bids per round reached")
                continue
            p.load_details(job)
            if job.extra.get("error"):
                R.event("job_error", **base, reason=job.extra["error"])
                p.cleanup(job)
                continue
            base.update(passengers=job.passengers, miles=job.miles, guide_min=job.guide_min, guide_max=job.guide_max)
            decision, reason, amount = decide(job, rules, now)
            if decision == "skip":
                self._skip(job, base, reason)
                p.cleanup(job)
                continue
            result = p.place_bid(job, amount, self.dry_run)
            event = ("bid_dry_run" if self.dry_run else "bid_placed") if result.ok else "bid_failed"
            R.event(event, **base, bid=amount, decision="bid" if result.ok else "failed", reason=reason,
                    details=result.message + (f" | screenshot: {result.screenshot}" if result.screenshot else ""))
            if result.ok:
                bids += 1
                R.update_job(job.ref, status="bid", bid=amount, bid_time=now.strftime("%Y-%m-%d %H:%M"),
                             miles=job.miles, guide=f"£{job.guide_min:.2f}-£{job.guide_max:.2f}",
                             reason=reason, passengers=job.passengers)
            else:
                fails = R.job(job.ref).get("bid_failures", 0) + 1
                R.update_job(job.ref, reason=result.message, bid_failures=fails,
                             gave_up=True if fails >= 2 else None)
                if fails >= 2:
                    R.event("bid_given_up", **base, reason=f"bid failed {fails} times – check this job on OTS by hand")
                if "captcha" in result.message.lower():
                    self.alert(f"OTS bidding paused: {result.message}. Please look at the bot's Chrome window.")
                    p.cleanup(job)
                    break
            p.cleanup(job)

    def _skip(self, job, base, reason) -> None:
        R = self.records
        if R.once(f"skip|{job.ref}|{_reason_key(reason)}"):
            R.event("bid_skipped", **base, decision="skip", reason=reason)
        # "pickup in X h" is NOT permanent: it is a cheap check, re-done every round.
        permanent = any(k in reason for k in PERMANENT_SKIPS)
        R.update_job(job.ref, status="skipped", reason=reason, miles=job.miles,
                     guide=(f"£{job.guide_min:.2f}-£{job.guide_max:.2f}" if job.guide_max else None),
                     skip_permanent=permanent or None)

    # ------------------------------------------------------------ email
    def email_round(self) -> None:
        """Handle every OTS availability / booking / re-confirmation email that the bot has
        not handled yet - unread OR already opened by someone - from the last
        email.lookback_days days. Its memory (state.json) stops anything twice."""
        R = self.records
        self.mail.refresh()
        if not self.site_ok("Email", self.mail.login_state):
            return
        listed = self.browser.js(
            "return [...document.querySelectorAll('div.list-rows div.list-item')].length") or 0
        rows = self.mail.ots_rows()
        handled = R.state["emails"]
        now_s = now_uk().strftime("%Y-%m-%d %H:%M")
        if not R.state.get("email_baseline"):
            # First run with this memory: emails that are already read were dealt with
            # before the bot (or by a person) - note them, never post old jobs.
            n = 0
            for r in rows:
                if not r["unread"]:
                    handled.setdefault("subj|" + r["subject"], "baseline " + now_s)
                    n += 1
            R.state["email_baseline"] = now_s
            R.save_state()
            R.event("email_baseline", platform="OTS",
                    details=f"first run: {n} already-read OTS emails noted as handled (not posted/alerted)")
        lookback = self.cfg["email"].get("lookback_days", 3)
        today = date.today()
        todo = []
        for r in rows:
            key = "subj|" + r["subject"]
            if key in handled and not (r["kind"] == "reconfirmation" and r["unread"]):
                continue
            if not r["unread"] and (r["day"] is None or (today - r["day"]).days > lookback):
                continue  # read and old (or undated): leave it
            todo.append(r)
        R.event("email_round", platform="OTS",
                details=f"{listed} emails listed after search, {len(rows)} OTS booking/availability emails, "
                        f"{len(todo)} new to handle ({sum(r['unread'] for r in todo)} unread)")
        for row in todo:
            mail = self.mail.read(row, restore_unread=self.dry_run and row["unread"])
            if not mail:
                if R.once(f"mailerr|{row['subject']}|{today}"):
                    R.event("email_error", platform="OTS", ref=row["ref"], email_subject=row["subject"],
                            reason="could not open/parse the email - will retry next round")
                continue
            if mail.key not in handled:
                self.handle_mail(mail, opened_by_someone=not row["unread"])
            handled["subj|" + mail.subject] = now_s
            handled[mail.key] = now_s
            R.save_state()

    def handle_mail(self, mail, opened_by_someone: bool) -> None:
        R = self.records
        f = mail.fields
        now = now_uk()
        note = " (email was already opened by someone)" if opened_by_someone else ""
        base = dict(platform="OTS", ref=mail.ref, email_subject=mail.subject)
        if mail.kind == "availability":
            exp = f.get("expires")
            R.update_job(mail.ref, status="availability_requested", availability_time=now.strftime("%Y-%m-%d %H:%M"))
            if exp and exp < now:
                R.event("email_availability", **base, bid=f.get("quoted"), status="availability_requested",
                        reason=f"deadline {fmt_uk(exp)} already passed - no alert, no post{note}")
                return
            R.event("email_availability", **base, bid=f.get("quoted"), status="availability_requested",
                    reason=f"answer by {fmt_uk(exp) or f.get('expires_text')}{note}")
            self.queue_alert(mail.ref, formatter.availability_alert(mail.ref, f, self.cfg["post"]),
                             key=f"avail|{mail.ref}", not_after=exp)
            if self.cfg["email"].get("post_on_availability", True):
                job = R.job(mail.ref)
                pf = formatter.fields_from_job(job, f.get("quoted") or job.get("bid"))
                if not pf:
                    R.event("post_skipped", **base, reason="no saved job details (the bot did not bid on this job) "
                                                           "- alert only; posts when the Job Confirmation arrives")
                elif pf["starting"] < now:
                    R.event("post_skipped", **base, reason="pickup time already passed")
                elif not job.get("posted_stage"):
                    text, price = formatter.booking_post(pf, job.get("miles"), now, self.cfg["post"], mail.ref)
                    R.update_job(mail.ref, posted_price=price, posted_stage="availability")
                    R.event("post_queued", **base, reason="availability request - posting from the bid's saved details",
                            details=f"post price £{price} (quoted £{f.get('quoted')} - {self.cfg['post']['price_minus']})")
                    self.queue_posts(mail.ref, text, not_after=pf["starting"])
        elif mail.kind == "booking":
            job = R.job(mail.ref)
            start = f.get("starting")
            text, price = formatter.booking_post(f, job.get("miles"), now, self.cfg["post"], mail.ref)
            R.update_job(mail.ref, status="booked", booked_time=now.strftime("%Y-%m-%d %H:%M"),
                         ots_total=f.get("total"), pickup=fmt_uk(start),
                         **{"from": f.get("pickup")}, to=f.get("dropoff"), vehicle=f.get("vehicle"))
            R.event("email_booking", **base, pickup=fmt_uk(start) or f.get("starting_text"),
                    **{"from": f.get("pickup")}, to=f.get("dropoff"), vehicle=f.get("vehicle"),
                    miles=job.get("miles"), bid=f.get("total"), status="booked",
                    details=f"post price £{price} (OTS £{f.get('total')} - {self.cfg['post']['price_minus']}){note}")
            if start and start < now:
                R.event("post_skipped", **base, reason="pickup time already passed")
            elif job.get("posted_stage"):
                R.event("post_skipped", **base, reason=f"already posted to the groups ({job['posted_stage']} stage)")
            else:
                R.update_job(mail.ref, posted_price=price, posted_stage="booking")
                self.queue_posts(mail.ref, text, not_after=start)
        elif mail.kind == "reconfirmation":
            start = f.get("starting")
            R.update_job(mail.ref, status="reconfirm_requested")
            if start and start < now:
                R.event("email_reconfirmation", **base, reason=f"pickup already passed - no alert{note}")
                return
            R.event("email_reconfirmation", **base, reason=f"changed: {f.get('changes')}{note}",
                    pickup=fmt_uk(start), **{"from": f.get("pickup")}, to=f.get("dropoff"),
                    status="reconfirm_requested")
            self.queue_alert(mail.ref, formatter.reconfirmation_alert(mail.ref, f, self.cfg["post"]),
                             key=f"reconf|{mail.ref}|{mail.key}", not_after=start)
            if self.cfg["email"].get("repost_reconfirmations"):
                job = R.job(mail.ref)
                text, _ = formatter.booking_post(f, job.get("miles"), now, self.cfg["post"], mail.ref)
                self.queue_posts(mail.ref, "UPDATED\n" + text, not_after=start, force=True)

    # ------------------------------------------------------------ WhatsApp outbox
    # Posts are queued in state.json first, so a crash or a WhatsApp hiccup never loses one.
    # Queueing is idempotent (never the same job to the same group twice) and every item
    # carries a 'not_after' time: if it could not be sent before then, it is dropped.
    def queue_posts(self, ref: str, text: str, not_after=None, force: bool = False) -> None:
        R = self.records
        job = R.job(ref)
        posted = set(job.get("groups_posted", []))
        waiting = {(i["ref"], i["target"]) for i in R.state["outbox"] if i["kind"] == "group"}
        n = 0
        for g in self.cfg["whatsapp"]["groups"]:
            if not force and (g in posted or (ref, g) in waiting):
                continue
            R.state["outbox"].append({"id": uuid.uuid4().hex[:8], "kind": "group", "target": g, "ref": ref,
                                      "text": text, "tries": 0,
                                      "not_after": not_after.isoformat() if not_after else None})
            n += 1
        R.save_state()
        if n < len(self.cfg["whatsapp"]["groups"]) and not force:
            R.event("post_dedup", ref=ref, details=f"{n} groups queued; the others already had (or are getting) this job")

    def queue_alert(self, ref: str, text: str, key: str | None = None, not_after=None) -> None:
        R = self.records
        alerts = R.state.setdefault("alerts", {})
        if key:
            if key in alerts:
                return
            alerts[key] = now_uk().strftime("%Y-%m-%d %H:%M")
        for c in self.cfg["whatsapp"].get("alert_contacts", []):
            R.state["outbox"].append(
                {"id": uuid.uuid4().hex[:8], "kind": "contact", "target": c["phone"], "name": c.get("name", ""),
                 "ref": ref, "text": text, "tries": 0, "not_after": not_after.isoformat() if not_after else None})
        R.save_state()
        desktop_alert("TS Minicab", text)

    def alert(self, text: str) -> None:
        self.queue_alert("", text)

    def send_outbox(self) -> None:
        R = self.records
        box = R.state["outbox"]
        if not box:
            return
        if not self.site_ok("WhatsApp", self.wa.login_state):
            return  # kept in the outbox; sent when WhatsApp is back (if still in time)
        done = []
        for item in list(box):
            target = item["target"] if item["kind"] == "group" else f"{item.get('name', '')} ({item['target']})"
            na = item.get("not_after")
            if na and datetime.fromisoformat(na) < now_uk():
                done.append(item["id"])
                R.event("whatsapp_dropped", platform="OTS", ref=item["ref"], wa_target=target,
                        reason=f"not sent in time (pickup/deadline {na[:16]} passed)", details=item["text"])
            else:
                if item["kind"] == "group":
                    ok, msg = self.wa.post_to_group(item["target"], item["text"], self.dry_run)
                    event = "whatsapp_post"
                else:
                    ok, msg = self.wa.message_number(item["target"], item["text"], self.dry_run)
                    event = "whatsapp_alert"
                item["tries"] += 1
                R.event(event if ok else event + "_failed", platform="OTS", ref=item["ref"], wa_target=target,
                        status="sent" if ok and not self.dry_run else ("dry run" if ok else "failed"),
                        reason=msg, details=item["text"])
                if ok:
                    done.append(item["id"])
                    if item["kind"] == "group" and item["ref"]:
                        j = R.job(item["ref"])
                        j.setdefault("groups_posted", [])
                        if item["target"] not in j["groups_posted"]:
                            j["groups_posted"].append(item["target"])
                elif item["tries"] >= 5:
                    done.append(item["id"])
                    R.event("whatsapp_gave_up", ref=item["ref"], wa_target=target, reason=f"5 failed tries: {msg}")
                    desktop_alert("TS Minicab - WhatsApp failed", f"Could not send to {target} after 5 tries.\n{msg}")
            R.state["outbox"] = [i for i in box if i["id"] not in done]
            R.save_state()
        self.wa.ensure_home()
