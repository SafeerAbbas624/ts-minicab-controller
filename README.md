# TS Minicab Controller

A 24/7 assistant for a London private-hire operator that works as a supplier on the
**OTS** transfer platform (suppliers.ots-system.net). It:

- **bids** on OTS quotation requests that match your rules,
- **reads** OTS emails in Titan webmail (Hostinger) – availability requests, job
  confirmations and re-confirmations,
- **posts** won jobs to your WhatsApp job groups and **alerts** your team on WhatsApp
  when OTS needs an answer,
- keeps an **Excel record** of everything it saw and did, one sheet per UK day.

It runs inside your normal, already logged-in Google Chrome through a small companion
extension, so there is no separate login and no password is ever stored by the bot.

> **Status:** in use since November 2026. Built for OTS + Titan + WhatsApp Web; the design
> lets more bidding platforms be added as plug-ins.

---

## Contents

- [How it works](#how-it-works)
- [Safety first](#safety-first)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [Running 24/7](#running-247)
- [The Excel record](#the-excel-record)
- [Project layout](#project-layout)
- [Adding another bidding platform](#adding-another-bidding-platform)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)

---

## How it works

Every round (default: every 2 minutes):

```
1. Check the three tabs are logged in
   (a logged-out site is skipped and you get one alert; everything else keeps running)

2. OTS – reload the "Unresponded" list, scroll until every job is loaded, then for each job:
     skip   if pickup is less than 24 h away (UK time)
     skip   if the route is more than 80 miles
     skip   if OTS shows no "we are expecting prices between £X and £Y"
     bid    £Y − 3
   Every decision is logged once, with its reason.

3. Titan – search the OTS sender and handle every OTS email not handled yet
   (unread, or already opened by someone on a phone) from the last 3 days:
     Journey Availability Request -> WhatsApp alert to your alert numbers
                                     + post the job to all groups (price = quoted − £15)
     Job Confirmation             -> post to all groups (OTS total − £15),
                                     unless it was already posted at the availability stage
     Job Re-Confirmation          -> WhatsApp alert to your alert numbers

4. WhatsApp – send everything waiting in the outbox

5. Write the Excel record, wait, repeat
```

Example group post (format and wording are set in `config.yaml`):

```
🚨 TOMORROW 23NOV AT 1:30 PM 🚨

	PICK UP • 2 Sample Street, Chelmsford, CM1 1AA

	DROP OFF •  Gatwick Airport, North Terminal

 Saloon Car

£73 NET
 POB PAYMENT

ABOUT 35 MILES
```

All rule times are **UK time** (GMT/BST), whatever time zone the PC is in.

---

## Safety first

- **It never confirms anything for you.** It never clicks *View And Respond*,
  *Confirm And View Details* or *Re-Confirm And View Details* in OTS emails. A person does that.
- **Dry-run mode** (the default in `config.example.yaml`) looks at everything and logs what
  it *would* do, without bidding or posting. Dry runs keep their own memory and Excel file.
- **Never twice:** a job is never bid on twice, never posted to the same group twice and
  never alerted twice – also after a crash or restart (posts wait in a saved outbox).
- **Never late:** nothing is posted or alerted for a job whose pickup (or answer deadline)
  has already passed.
- **Alerts are checked by phone number:** before messaging an alert contact, the bot opens
  that chat's contact info and checks the number, not the saved name.
- **No CAPTCHA tricks:** if OTS ever shows a reCAPTCHA challenge, bidding stops for that
  round and you are alerted.
- **Private data stays on your PC:** your real `config.yaml` (phone numbers, groups), the
  extension's token and the `records/` folder are in `.gitignore`.

---

## Requirements

- Windows 10/11 (other systems should work but are untested)
- Google Chrome 116 or newer
- Python 3.12 or newer – tick *Add python.exe to PATH* when installing
- Accounts that are already logged in, in one Chrome profile:
  - OTS supplier portal – suppliers.ots-system.net
  - Titan webmail – hostinger.titan.email
  - WhatsApp Web – web.whatsapp.com (WhatsApp or WhatsApp Business)

---

## Installation

```powershell
git clone https://github.com/SafeerAbbas624/ts-minicab-controller.git
cd ts-minicab-controller
python -m pip install -r requirements.txt
copy config.example.yaml config.yaml
python run_bot.py --selftest
```

1. **Fill in `config.yaml`** (Notepad is fine): your WhatsApp group names and alert phone
   numbers. `python run_bot.py --list-chats` prints every chat name exactly as WhatsApp
   shows it.
2. **Make the extension's secret token:**
   ```powershell
   python run_bot.py --install-extension
   ```
3. **Load the extension** in the Chrome profile that is logged in to the three sites:
   open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked** and
   choose the `chrome-extension` folder. Pin it – its badge shows **on** while the bot is
   connected.
4. **Test without doing anything real:**
   ```powershell
   python run_bot.py --dry-run --once
   ```
   Then look at `records/controller_log_DRYRUN.xlsx`.

### Why an extension?

Chrome 136+ refuses automation tools (Selenium, Playwright) on its normal profile folder.
The *TS Minicab Bot Bridge* extension lets the bot work in your real, logged-in profile
instead: it only talks to the bot on `127.0.0.1`, and both sides prove they know the
token before any command is accepted. Chrome shows a bar "*TS Minicab Bot Bridge started
debugging this browser*" while it works – that is expected; don't press **Cancel**.

Prefer a separate browser? Set `chrome.mode: selenium` – the bot then starts its own Chrome
with its own profile folder, where you log in once (or copy a profile with
`python run_bot.py --setup-profile` while Chrome is closed).

---

## Configuration

Everything lives in `config.yaml`. Restart the bot after a change – at start-up it notices
changed bidding rules and re-checks jobs it skipped under the old rules.

| Setting | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | `false` = real bids and real WhatsApp messages |
| `loop_interval_seconds` | `120` | time between rounds |
| `bidding.ots.min_hours_before_pickup` | `24` | only bid this many hours (or more) before pickup – e.g. 12 or 6 |
| `bidding.ots.max_miles` | `80` | only bid on routes up to this distance |
| `bidding.ots.bid_below_max_guide` | `3` | bid = top of OTS's price range minus this |
| `bidding.ots.skip_vehicles` | `[]` | vehicle types never to bid on, e.g. `['8 Seater Minibus']` |
| `bidding.ots.min_bid` / `max_bids_per_round` | `10` / `30` | safety limits |
| `email.lookback_days` | `3` | handle OTS emails up to this old, read or unread |
| `email.post_on_availability` | `true` | post to groups as soon as OTS asks for availability |
| `email.repost_reconfirmations` | `false` | re-post a job when the customer changes it |
| `whatsapp.groups` | – | exact group names to post jobs to |
| `whatsapp.alert_contacts` | – | numbers (digits only, e.g. `447000000001`) to alert |
| `post.price_minus` | `15` | price in the post = OTS price minus this |
| `post.template` | – | the post format; fields `{day_word} {date} {time} {pickup} {dropoff} {vehicle} {price} {miles_line}` |
| `post.morning_until` / `tonight_from` | `12:00` / `18:00` | when the header says TODAY MORNING / TODAY / TONIGHT |

---

## Running

```powershell
python run_bot.py --dry-run --once       # one round, nothing real
python run_bot.py --dry-run              # keep going, nothing real
python run_bot.py --live                 # REAL bids and posts (asks you to type LIVE)
python run_bot.py --live --minutes 60    # live for one hour, then stop
python run_bot.py --live --only email    # only email -> WhatsApp, no bidding
python run_bot.py --live --only bid      # only bidding
python run_bot.py --list-chats           # print WhatsApp chat names (marks the ones in config)
python run_bot.py --selftest             # offline tests, nothing opened or sent
```

Stop with **Ctrl+C**.

### What the window shows

Every line starts with the UK time. Important events are coloured (green = bid placed /
message sent, magenta = OTS email, red = problem), and each round ends with a summary:

```
21:43:34  BOT STARTED    LIVE (real bids and posts) | rules: pickup >= 24 h, <= 80 mi, bid = top of guide - 3 | 25 WhatsApp groups, 2 alert numbers | every 120 s
21:49:52  OTS LIST       OTS                         5 jobs in the Unresponded list
21:49:52    - OTS-…-85690-1   Fri 27 Nov 14:55 | Gatwick Airport, North Terminal -> Dartford | Saloon Car | skip: pickup in 17.1 h (< 24 h)
21:49:52    - OTS-…-261-1     Thu 24 Dec 12:45 | Chislehurst -> Winchester | Executive Car | skip: 89 miles (> 80)
20:02:29  BID PLACED     OTS-…-70129-1  £80.00 | Sat 05 Dec 09:20 | Portsmouth -> Gatwick Airport | Saloon Car | 67 mi | OTS guide £70.00-£83.00 | OTS: price box gone
21:49:56  EMAIL CHECK    OTS                         37 emails listed after search, 8 OTS booking/availability emails, 0 new to handle
21:49:56  ROUND 3 DONE (LIVE) | OTS: 5 jobs listed, 0 bid, 0 failed, 5 skipped, 0 already bid | Email: 8 OTS emails, 0 new | WhatsApp: 0 posted, 0 alerts, 0 waiting | errors: 0 | took 8s, next round 21:51:48 UK
```

The same lines go to `records/bot.log`. The per-job lines are console/log only; the Excel
record gets one row per real event.

What a **dry run** touches: OTS – opens each job to read it (OTS marks it "viewed");
Titan – opens unhandled OTS emails and marks unread ones unread again; WhatsApp – only
checks the groups exist, never opens a chat or types.

---

## Running 24/7

1. Set `dry_run: false` in `config.yaml`.
2. Double-click `start_bot.bat`. It restarts the bot by itself if it ever stops with an error.
3. To start automatically after a reboot, add `start_bot.bat` to **Task Scheduler**
   (trigger: *At log on*).
4. On that PC: **Settings → System → Power → Sleep: Never**, set Windows Update *active
   hours*, and keep Chrome open (not minimised).

The bot brings its Chrome window to the front for a moment when it works in WhatsApp or
presses Submit on a bid – Chrome stops redrawing covered windows, and WhatsApp's chat list
would not load otherwise.

### Moving to another PC

Stop the bot on the old PC first (two copies would both bid and post). Install as above on
the new PC, copy your `config.yaml`, and copy the `records/` folder if you want to keep the
bot's memory and Excel history. Make a new token there with `--install-extension`.

---

## The Excel record

`records/controller_log.xlsx` – one sheet per UK day plus a **Job status** sheet.

Each day sheet has one row per event: time (UK and PC), platform, event, job reference,
pickup, from, to, vehicle, passengers, miles, OTS price guide, bid, decision, reason, email
subject, WhatsApp target, status and details.

| Event | Meaning |
|---|---|
| `job_seen`, `bid_skipped` | a new job, and why it was not bid on |
| `bid_placed`, `bid_failed`, `bid_given_up` | bids (a job is given up after 2 failed attempts) |
| `bid_not_registered` | a job is still "Unresponded" after our bid – check it by hand |
| `email_availability`, `email_booking`, `email_reconfirmation` | OTS emails handled |
| `whatsapp_post`, `whatsapp_alert` (`_failed`, `_dropped`) | messages sent, failed or too late |
| `login_required`, `login_ok` | a site logged out / back |
| `rules_changed` | bidding rules changed at start-up |
| `error` | something unexpected; screenshot in `records/screenshots/` |

The **Job status** sheet follows each job across days:
`bid` → `availability_requested` → `booked` → `reconfirm_requested`.

Opening the Excel file while the bot runs is fine: Excel locks the file, so the bot keeps
new rows waiting and writes them once you close it. Every event is also written straight
away to `records/events.jsonl`.

---

## Project layout

```
run_bot.py                 command line entry point
start_bot.bat              24/7 starter with automatic restart
config.example.yaml        settings template (copy to config.yaml)
bot/
  controller.py            the main loop: logins, bidding, emails, outbox
  rules.py                 the bidding rules (pure functions)
  platforms/base.py        plug-in contract for bidding platforms
  platforms/ots.py         OTS adapter
  email_titan.py           Titan webmail: find, open and parse OTS emails
  whatsapp.py              WhatsApp Web: find groups, check numbers, send
  formatter.py             post and alert text
  browser.py               browser layer: extension bridge or Selenium, via Chrome DevTools Protocol
  bridge.py                local WebSocket server the extension connects to
  records.py               Excel record, events.jsonl and the bot's memory (state.json)
  timeutil.py              UK time parsing
chrome-extension/          the "TS Minicab Bot Bridge" extension
tests/                     offline self-tests (fake inbox and fake WhatsApp)
SELECTORS.md               how each site's pages are built and what the bot reads
```

---

## Adding another bidding platform

1. Create `bot/platforms/<name>.py` with a class that subclasses `BiddingPlatform` and
   implements `login_state`, `refresh`, `list_jobs`, `load_details` and `place_bid`
   (`place_bid` must do nothing real when `dry_run` is true).
2. Register it in `PLATFORMS` in `bot/platforms/__init__.py`.
3. In `config.yaml` add its URL under `urls:` and a `bidding: <name>:` block with
   `enabled: true` and the same rule keys as `ots`. The bot opens a tab for it automatically.
4. If the platform sends booking emails, add a parser next to `parse_body` in
   `bot/email_titan.py` and its subject patterns under `email:`.

The shared rules in `bot/rules.py` and the whole email → WhatsApp → Excel pipeline work
for every platform.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| "Waiting for Chrome…" | Open Chrome with the profile that has the extension; check its badge says **on** |
| `config.js is missing` | `python run_bot.py --install-extension`, then reload the extension in `chrome://extensions` |
| A bot tab was closed by accident | Nothing to do – before each step the bot checks its tabs and reopens a missing one (logins live in the Chrome profile) |
| Chrome was closed | A PC popup says the controller is paused; open Chrome again with the same profile and it carries on by itself |
| A site keeps saying logged out | Log in again in the bot's Chrome window; the bot carries on by itself |
| A WhatsApp group "NOT found" | Names must match exactly – run `--list-chats` and copy the name |
| Something looks wrong | `records/bot.log`, the `error` rows in Excel and `records/screenshots/` |
| A site changed its layout | Update the `SEL_*` / `JS_*` constants at the top of that site's file, using `SELECTORS.md` |

---

## Disclaimer

This is an independent tool. It is not affiliated with or endorsed by OTS (Onward Travel
Solutions), Hostinger/Titan, WhatsApp or Google. Automating websites may be restricted by
their terms of use – check them, keep dry-run on until you are happy, and use it at your
own risk.
