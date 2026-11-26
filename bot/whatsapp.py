"""WhatsApp Web. See SELECTORS.md section 3.

ChromeDriver cannot type emoji or the maths-bold letters used in some group names,
so chats are found by scrolling the chat list and matching the exact title, and
messages are pasted (a synthetic paste event) rather than typed. UI text is never
matched, so the Italian interface does not matter."""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

SEL_PANE = "#pane-side"
SEL_COMPOSE = '#main footer div[contenteditable="true"]'
SEL_SEARCH = 'input[data-tab="3"]'

# The FIRST phone number shown outside the chat itself and the chat list – i.e. the number
# under the person's name at the top of the contact info panel (opened from the chat
# header). Further down that panel "groups in common" list hundreds of member numbers,
# so only the first one counts. Digits only.
JS_INFO_NUMBERS = r"""
const out = [];
const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
while (w.nextNode()) {
  const el = w.currentNode.parentElement;
  if (!el || el.closest('#main') || el.closest('#pane-side')) continue;
  const m = w.currentNode.textContent.match(/\+\d[\d \-]{6,}\d/g);
  if (m) return m[0].replace(/\D/g, '');
}
return null;
"""

# In search results: section heading rows have no title. Return the first titled row of the
# first section ("Chat") – a person, never a row under "groups in common".
JS_SEARCH_CONTACT = """
const click = arguments[0];
let headings = 0;
for (const row of document.querySelectorAll('#pane-side [role=row]')) {
  const t = [...row.querySelectorAll('span[title]')].map(s => s.getAttribute('title'))
            .find(x => x && !x.startsWith('\u202a'));
  if (!t) { headings++; if (headings > 1) return null; continue; }
  if (headings === 1) return click ? (row.querySelector('[role=gridcell]') || row) : t;
}
return null;
"""

JS_STATE = """
if (document.querySelector('#pane-side')) return 'in';
if (document.querySelector('[data-ref] canvas, canvas[aria-label]')) return 'out';
return 'loading';
"""

# Find a chat row by exact title in what is currently rendered; click it.
JS_FIND_ROW = """
const name = arguments[0], click = arguments[1];
for (const row of document.querySelectorAll('#pane-side [role=row]')) {
  const t = [...row.querySelectorAll('span[title]')].map(s => s.getAttribute('title'))
            .find(x => x && !x.startsWith('\\u202a'));
  if (t === name) return click ? (row.querySelector('[role=gridcell]') || row) : true;
}
return false;
"""

JS_SCROLL_PANE = """
const p = document.querySelector('#pane-side');
if (!p) return -1;
if (arguments[0] === 0) { p.scrollTop = 0; return 0; }
const before = p.scrollTop; p.scrollTop = before + arguments[0];
return p.scrollTop === before ? -1 : p.scrollTop;
"""

JS_HEADER = """
const h = document.querySelector('#main header');
if (!h) return '';
const s = [...h.querySelectorAll('span[title], span[dir=auto]')].map(x => x.getAttribute('title') || x.innerText);
return (s.find(Boolean) || h.innerText.split('\\n')[0] || '').trim();
"""

JS_PASTE = """
const box = document.querySelector('#main footer div[contenteditable="true"]');
if (!box) return false;
box.focus();
const dt = new DataTransfer();
dt.setData('text/plain', arguments[0]);
box.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
return true;
"""

JS_COMPOSE_TEXT = """
const box = document.querySelector('#main footer div[contenteditable="true"]');
return box ? box.innerText : null;
"""

JS_LAST_OUT = """
// Text of the last few chat rows (WhatsApp no longer marks sent bubbles with .message-out).
return [...document.querySelectorAll('#main [role=row]')].slice(-4).map(r => r.innerText).join(' | ');
"""


def _norm(s: str) -> str:
    return " ".join((s or "").split())


class WhatsApp:
    tab_name = "whatsapp"

    def __init__(self, browser, cfg: dict):
        self.browser = browser
        self.cfg = cfg
        self.wcfg = cfg["whatsapp"]

    def login_state(self) -> str:
        self.browser.tab(self.tab_name)
        return self.browser.js(JS_STATE)

    def ensure_home(self) -> None:
        """If an alert navigated to /send?phone=..., go back to the normal page."""
        b = self.browser
        b.tab(self.tab_name)
        if "/send" in b.current_url():
            b._get(self.cfg["urls"]["whatsapp"])
        b.wait_for(lambda: self.login_state() == "in", timeout=90)

    # ------------------------------------------------------------ find chats
    def find_chat(self, name: str, click: bool) -> bool:
        b = self.browser
        b.tab(self.tab_name)
        b.show()
        if b.js(JS_SCROLL_PANE, 0) == -1:   # chat list not there (still loading?)
            b.wait_for(lambda: self.login_state() == "in", timeout=60)
            b.js(JS_SCROLL_PANE, 0)
        time.sleep(0.4)
        def hit() -> bool:
            if click:
                return b.click_js(JS_FIND_ROW, name, True)
            return bool(b.js(JS_FIND_ROW, name, False))
        for _ in range(150):
            if hit():
                return True
            if b.js(JS_SCROLL_PANE, 500) == -1:
                break
            time.sleep(0.25)
        for _ in range(4):  # the last rows render a moment after the final scroll
            time.sleep(0.8)
            if hit():
                return True
        return False

    def open_chat(self, name: str) -> bool:
        if not self.find_chat(name, click=True):
            return False
        ok = self.browser.wait_for(lambda: _norm(self.browser.js(JS_HEADER)) == _norm(name)
                                   and self.browser.js(JS_COMPOSE_TEXT) is not None, timeout=10)
        return bool(ok)

    def list_groups(self) -> list[str]:
        """All chat titles in the list (used by --list-chats to check group names)."""
        b = self.browser
        b.tab(self.tab_name)
        names: list[str] = []
        b.show()
        b.js(JS_SCROLL_PANE, 0)
        time.sleep(0.5)
        for _ in range(300):
            for t in b.js("""return [...document.querySelectorAll('#pane-side [role=row]')].map(r =>
                    [...r.querySelectorAll('span[title]')].map(s => s.getAttribute('title'))
                    .find(x => x && !x.startsWith('\\u202a')) || '')""") or []:
                if t and t not in names:
                    names.append(t)
            if b.js(JS_SCROLL_PANE, 500) == -1:
                break
            time.sleep(0.3)
        b.js(JS_SCROLL_PANE, 0)
        return names

    # ------------------------------------------------------------ send
    def _send_in_open_chat(self, text: str) -> tuple[bool, str]:
        b = self.browser
        if b.js(JS_COMPOSE_TEXT) is None:
            return False, "compose box not found"
        if _norm(b.js(JS_COMPOSE_TEXT)):
            return False, "compose box already had text – not sending"
        b.js(JS_PASTE, text)
        pasted = b.wait_for(lambda: _norm(b.js(JS_COMPOSE_TEXT)), timeout=5)
        first_line = next((l for l in text.splitlines() if l.strip()), "")
        if not pasted or _norm(first_line)[:15] not in _norm(pasted):
            return False, "paste did not land in the compose box – not sending"
        if not b.click_js("return document.querySelector(arguments[0]);", SEL_COMPOSE):
            return False, "could not focus the compose box – not sending"
        b.press("Enter")
        sent = b.wait_for(lambda: not _norm(b.js(JS_COMPOSE_TEXT)), timeout=10)
        if not sent:
            return False, "message stayed in the compose box after Enter"
        time.sleep(1.5)
        last = _norm(b.js(JS_LAST_OUT))
        if _norm(first_line)[:15] not in last:
            return True, "sent (could not confirm it in the chat view)"
        return True, "sent"

    def post_to_group(self, name: str, text: str, dry_run: bool) -> tuple[bool, str]:
        self.ensure_home()
        if dry_run:
            found = self.find_chat(name, click=False)
            return found, ("DRY RUN – group found, message not sent" if found
                           else "DRY RUN – group NOT found in chat list")
        if not self.open_chat(name):
            return False, "group not found or did not open"
        ok, msg = self._send_in_open_chat(text)
        time.sleep(self.wcfg.get("seconds_between_posts", 4))
        return ok, msg

    def _clear_search(self) -> None:
        b = self.browser
        if b.click_js("return document.querySelector(arguments[0]);", SEL_SEARCH):
            b.press("SelectAll")
            b.press("Backspace")
            b.press("Escape")
            time.sleep(0.5)

    def open_by_search(self, phone: str) -> str | None:
        """Type the number into WhatsApp's search and click the person under 'Chat'
        (never a group under 'groups in common'). Returns the chat name, or None."""
        b = self.browser
        self.ensure_home()
        b.show()
        if not b.click_js("return document.querySelector(arguments[0]);", SEL_SEARCH):
            return None
        try:
            b.press("SelectAll")
            b.press("Backspace")
            b.insert_text("+" + phone)
            title = b.wait_for(lambda: b.js(JS_SEARCH_CONTACT, False), timeout=8, every=0.5)
            if not title or not b.click_js(JS_SEARCH_CONTACT, True):
                return None
            ok = b.wait_for(lambda: _norm(b.js(JS_HEADER)) == _norm(title)
                            and b.js(JS_COMPOSE_TEXT) is not None, timeout=10)
            return title if ok else None
        finally:
            self._clear_search()

    def open_chat_number(self) -> str:
        """The open chat's own phone number (digits only), from its contact info panel.
        Opens the panel by clicking the chat header, reads it, closes it again."""
        b = self.browser
        if not b.click_js("return document.querySelector('#main header [role=button]') "
                          "|| document.querySelector('#main header');"):
            return ""
        try:
            return b.wait_for(lambda: b.js(JS_INFO_NUMBERS), timeout=8, every=0.5) or ""
        finally:
            b.press("Escape")
            time.sleep(0.8)

    def message_number(self, phone: str, text: str, dry_run: bool) -> tuple[bool, str]:
        """Direct message to a phone number (alerts): search the number, click the contact,
        check the chat's own phone number (names differ between phones, numbers don't),
        paste, send. If search or the number check fails, opens
        web.whatsapp.com/send?phone=<number> and checks the number again."""
        if dry_run:
            return True, "DRY RUN – alert not sent"
        b = self.browser
        want = re.sub(r"\D", "", phone)
        title = self.open_by_search(phone)
        if not title:  # first search right after WhatsApp comes to the front can be too early
            time.sleep(2)
            title = self.open_by_search(phone)
        how = f"via search (chat '{title}')"
        if title and self.open_chat_number() != want:
            log.warning("search result %r is not +%s – using the direct link instead", title, want)
            title = None
        if not title:
            b.tab(self.tab_name)
            b._get(f"https://web.whatsapp.com/send?phone={want}")
            b.show()
            if not b.wait_for(lambda: b.js(JS_COMPOSE_TEXT) is not None, timeout=60, every=1):
                return False, "number not found in search and its chat did not open (not on WhatsApp?)"
            time.sleep(1)
            seen = self.open_chat_number()
            if seen != want:
                return False, f"opened chat shows number {('+' + seen) if seen else 'none'}, not +{want} – not sending"
            how = "via direct link"
        ok, msg = self._send_in_open_chat(text)
        return ok, f"{msg} {how}, number +{want} checked"
