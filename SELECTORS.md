# Selectors and page structure (inspected 26 Nov 2026)

Inspected live in a Chrome profile logged in to the three sites. Nothing was bid,
confirmed or sent during inspection.
All selectors live in code in one place per site (`bot/platforms/ots.py`,
`bot/email_titan.py`, `bot/whatsapp.py`) as `SEL_*` constants — update them there
if a site changes.

---

## 1. OTS supplier portal — https://suppliers.ots-system.net/  (v2.8.0)

Single-page React app. No per-job URL: everything happens on `/`. Each opened job
becomes a "tab" in the top bar (`ul.tabs li.tab`), which persists across reloads.
Job data is already in the page after load (no separate XHR when a job is opened).
The page loads Google reCAPTCHA Enterprise (invisible) – it runs inside the normal
browser; the bot does not touch it, and stops + alerts if any visible challenge appears.

All times on OTS are UK time with an explicit zone suffix, e.g. `(GMT)` (will be
`(BST)` in summer). The PC clock may be in any time zone – the bot always converts
to Europe/London, never uses the PC's local time.

### Logged in vs logged out
| state | marker |
|---|---|
| logged in | `input[name="combinedFilter"]` exists (the filter bar), title `Quotation Requests - OTS Portal` |
| logged out | any visible `input[type="password"]`, or no filter bar after 30 s |

### Quotation list (left panel)
| what | selector / text |
|---|---|
| list panel | first `.scroll-panel` (its scroller: first `.scroll-container`) |
| filter "Show" | `input[name="combinedFilter"]` — values: `Unresponded` (default) / `Responded` / `Rejected` / `All` |
| sort | `input[name="combinedSort"]` = `Expires (Earliest)` |
| read status | `input[name="combinedSeen"]` = `Show All` |
| reference search | `input[name="referenceSearch"]`, direction `input[name="referenceDirection"]` = `Ending` |
| refresh | element with text `Refresh` in the filter bar (`.action` / `.has-click-handler`) |
| job row | `div.clickable > form.row` (class `row primary status-primary`) — click the parent `div.clickable` to open |
| paging | infinite scroll; footer `span` text `You have loaded N of M Quotation Requests…` — scroll the container to the bottom until N == M |

Row text (4 lines):
```
OTS-26112026152627-86434-1
Pick up at 10:00 am, Fri 27th Nov 2026 (GMT)
From Dover Ferry Terminal, Dover, CT17 9TF to 1 Example Road, London, SW1A 1AA by Executive Car
Expires on 20:08 pm, Thu 26th Nov 2026 (GMT)
```
Times are also in `time.datetime[datetime]` (same text format, not ISO).
Note the odd 24h+am/pm mix: `20:40 pm`, `13:30pm`. Parser: if hour > 12 ignore am/pm.

### Job detail panel (right side, opens on click)
Find it as the visible `.scroll-panel` (not the first) whose text contains the job ref.
It has 4 `form.row.primary` blocks: details, notes, important notice, submit price.

| what | text / selector |
|---|---|
| route | `Route` block: two lines, pickup then drop-off |
| miles | `This route is roughly 64 miles` (`div.sidebar p.text span`) — always miles in every job seen; never km |
| pickup | `Pick up at 10:00 am, Fri 27th Nov 2026 (GMT)` |
| passengers | `1 Passenger with Suitcases` / `2 Passengers with Hand Luggage` |
| vehicle | `Saloon Car` / `Executive Car` / `Estate Car` … |
| notes | `Notes from OTS` block |
| price guide | `As a guide, we are expecting prices between <strong>£103.00</strong> and <strong>£121.00</strong>.` (`form.row.primary > p.text > span`) |
| tender expiry | `This tender request will expire on 20:08 pm, Thu 26th Nov 2026 (GMT).` |
| price box | `input[name="fare"]` (type number, prefix £) |
| clear button | `button.button.bg-primary-dark` (text `Clear`) |
| **submit bid** | `button[type="submit"]` in the same form (text `Submit`) — **real bid** |
| close job tab | `ul.tabs li.tab` containing the ref → `svg title` "Cross Icon" → parent `i.icon` |

**Live-tested 27 Nov 2026.** The Submit button sits in the bottom-right corner, fully
covered by the reCAPTCHA badge iframe, so a mouse click lands on the badge (first live
bid did NOT go through). The bot types the price and presses **Enter** in the price box,
which submits the form. After a good bid the "Submit Price" section turns into
"Loading…" (price box gone) and the job leaves the `Unresponded` list on reload.
No visible change after Enter = bid not sent (logged as `bid_failed`).

Main menu (☰): Quotation Requests, Communications, Users, Home, Cookies, Terms,
Invoicing, Invoice History, My Profile, Quality Score, Log Out. There is **no bookings
page** – accepted jobs arrive by email only.

---

## 2. Titan mail — https://hostinger.titan.email/mail/

| what | selector |
|---|---|
| logged in | `div.list-rows` present; URL stays on `/mail/` |
| logged out | URL contains `/login` or a visible `input[type="password"]` |
| inbox folder | left nav text `Inbox` |
| email row | `div.list-rows div.list-item` |
| unread flag | `span.unread-true` (read: `span.unread-false`) inside the row |
| sender | `.list-column-Participants` |
| subject | `span.subject` (inside `.list-column-Message`) |
| date | `.list-column-Date` (PC local time, e.g. `8:40 pm` or `Nov 25`) |
| open email | click `.list-column-Message` — **opening marks it read** |
| body | first `<iframe>` (same origin) → `contentDocument.body.innerText` |
| mark unread | toolbar `button.btn-unread` (used in dry-run to put emails back) |

### OTS emails (sender "Onward Travel Solutions")
| subject | meaning | bot action |
|---|---|---|
| `Journey Quotation Request - OTS-…` | new job to quote (same as portal list) | ignore |
| `Journey Availability Request - OTS-…` | we were picked; must click "View And Respond" → Available within ~30 min | **alert** 2 contacts on WhatsApp (never clicks) |
| `Job Confirmation - OTS-…` | job assigned to us; button `Confirm And View Details` | **post** to all groups (never clicks confirm) |
| `Job Re-Confirmation - OTS-…` | customer changed the booking; button `Re-Confirm And View Details` | **alert** contacts (configurable: also re-post) |
| `Forthcoming Journeys (Correct as of …)` | digest | ignore |

Job Confirmation body (key lines):
```
Job Confirmation
Your quotation of £88.00 has been accepted and OTS-…-29191-1 has been assigned to you:
Pick Up: 2 Sample Street, Chelmsford, CM1 1AA
Drop Off: Gatwick Airport, North Terminal
Vehicle	Saloon Car
Starting	Monday 23rd November 2026, 13:30pm
Total	£88.00
Confirm And View Details
```
Passenger name/phone/flight are **not** in the email – only behind the confirm link.
Links go via `tracking.ots-system.net/tracking/click` – the bot never follows them.

Availability Request body: `We would like to know if you are able to carry out OTS-…, you recently quoted £100.00.`
… `This request will expire on Friday 20th November 2026, 19:07pm (approximately 29 minutes from now)`.

---

## 3. WhatsApp Web — https://web.whatsapp.com/  (WhatsApp Business, UI in **Italian**)

All selectors avoid UI text so the language does not matter.

| what | selector |
|---|---|
| logged in | `#pane-side` (chat list, `[role=grid]`) |
| logged out | QR code: `canvas` inside `[data-ref]`, or no `#pane-side` after 60 s |
| loading splash | neither of the above yet – keep waiting |
| search box | `input[data-tab="3"]` (role textbox) |
| chat row | `#pane-side [role=row]`; chat name = `span[title]` in the row whose title does not start with U+202A (those are message previews) |
| groups filter chip | button in group `Filtri chat` whose text starts `Gruppi` (index 4 of the chips) – used only for scraping names |
| open chat header | `#main header` (first line = chat name) |
| compose box | `#main footer div[contenteditable="true"][data-tab="10"]` (Lexical editor) |
| send | press Enter in the compose box; new lines with Shift+Enter |
| last messages (send check) | last 4 `#main [role=row]` texts – sent bubbles no longer have a `.message-out` class |
| alert to a number | type `+<digits>` in the search box → results start with a heading row "Chat" (no title), then the person, then heading "Gruppi in comune" and shared groups → click the first titled row of the first section → click `#main header` to open contact info → the FIRST `+…` number outside `#main`/`#pane-side` is the contact's own number (later ones are members of "groups in common") → must equal the alert number, Escape, paste, Enter, clear the search |
| direct chat by number | `https://web.whatsapp.com/send?phone=<digits>` (fallback when search finds nothing; reloads WhatsApp) |

**Hidden window gotcha:** when the Chrome window is covered or minimised, the page is
`visibilityState: hidden` and WhatsApp stops rendering chat-list rows on scroll (only ~22
rows ever exist). The bot calls CDP `Page.bringToFront` before any WhatsApp work.

Group names scraped 26 Nov 2026 (25, all chosen by the user) are in `config.yaml`.
Several use maths-bold Unicode letters (e.g. `𝗚𝗔𝗧𝗪𝗜𝗖𝗞 ⇋ …™`) – the bot matches the exact title.
All 25 were found by the bot's start-up check on 26 Nov 2026 (extension mode).
