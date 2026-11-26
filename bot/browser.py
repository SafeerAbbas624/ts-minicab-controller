"""Browser control. Two back-ends with the same interface:

* ExtensionBrowser (chrome.mode: extension, default) – works inside your normal, already
  logged-in Chrome profile through the 'TS Minicab Bot Bridge' extension.
  Opens its own Chrome window with the
  bot's tabs (re-uses an already open WhatsApp tab to avoid "WhatsApp open in another window").
* SeleniumBrowser (chrome.mode: selenium) – starts a separate Chrome with its own profile
  folder (Chrome 136+ refuses automation on the normal profile folder).

Everything on the pages goes through the Chrome DevTools Protocol: page scripts with
Runtime.evaluate (not blocked by site CSP) and real mouse/keyboard with Input.*.
Page scripts are function bodies using `arguments[i]` and `return`, and must return
plain data (no DOM elements)."""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class BrowserLost(RuntimeError):
    """The browser / extension went away; the controller restarts the browser layer."""


class JSError(RuntimeError):
    pass


KEYS = {
    "Enter": dict(key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r"),
    "Backspace": dict(key="Backspace", code="Backspace", windowsVirtualKeyCode=8),
    "Delete": dict(key="Delete", code="Delete", windowsVirtualKeyCode=46),
    "Escape": dict(key="Escape", code="Escape", windowsVirtualKeyCode=27),
    "SelectAll": dict(key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=2, commands=["selectAll"]),
    "ShiftEnter": dict(key="Enter", code="Enter", windowsVirtualKeyCode=13, modifiers=8),
}


class BaseBrowser:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tabs: dict[str, object] = {}

    # --- implemented by back-ends
    def open_tabs(self, names: list[str]) -> None: ...
    def tab(self, name: str): ...
    def _get(self, url: str) -> None: ...
    def reload(self, name: str) -> None: ...
    def cdp(self, method: str, params: dict | None = None) -> dict: ...
    def current_url(self) -> str: ...
    def quit(self) -> None: ...

    # --- shared
    def js(self, script: str, *args):
        expr = f"(async function(){{\n{script}\n}}).apply(null, {json.dumps(list(args), ensure_ascii=False)})"
        r = self.cdp("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True,
                                          "userGesture": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise JSError((d.get("exception") or {}).get("description") or d.get("text") or "page script error")
        return (r.get("result") or {}).get("value")

    def wait_for(self, fn, timeout: float = 30, every: float = 0.5):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = fn()
                if last:
                    return last
            except (JSError, KeyError, TypeError):
                pass
            time.sleep(every)
        return last

    def show(self) -> None:
        """Bring the current tab's window to the front. Chrome treats a covered window as
        'hidden' and WhatsApp then stops redrawing its chat list, so WhatsApp work (and real
        bid clicks) happen with the window in front. Other work runs in the background."""
        try:
            self.cdp("Page.bringToFront", {})
            time.sleep(1.0)
        except Exception as e:
            log.warning("could not bring window to front: %s", e)

    def click_at(self, x: float, y: float) -> None:
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": t, "x": x, "y": y}
            if t != "mouseMoved":
                p.update(button="left", clickCount=1)
            self.cdp("Input.dispatchMouseEvent", p)
            time.sleep(0.03)

    def click_js(self, locate_script: str, *args) -> bool:
        """locate_script returns an element (or null). It is scrolled into view and clicked
        with a real mouse click – only at a point where the element itself is on top
        (not covered by e.g. a reCAPTCHA badge). Returns False if no such point exists."""
        r = self.js(f"""
            const el = (function(){{ {locate_script} }}).apply(null, arguments);
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const b = el.getBoundingClientRect();
            if (!b.width || !b.height) return null;
            for (const [fx, fy] of [[0.5,0.5],[0.2,0.5],[0.1,0.5],[0.8,0.5],[0.5,0.2],[0.5,0.8]]) {{
              const x = b.left + b.width * fx, y = b.top + b.height * fy;
              if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
              const top = document.elementFromPoint(x, y);
              if (top && (top === el || el.contains(top))) return {{x, y}};
            }}
            return {{covered: true}};
        """, *args)
        if not r or r.get("covered"):
            if r:
                log.warning("click target is covered by another element – not clicking")
            return False
        time.sleep(0.2)
        self.click_at(r["x"], r["y"])
        return True

    def press(self, name: str) -> None:
        k = dict(KEYS[name])
        self.cdp("Input.dispatchKeyEvent", {"type": "keyDown", **k})
        k.pop("text", None)
        k.pop("commands", None)
        self.cdp("Input.dispatchKeyEvent", {"type": "keyUp", **k})

    def insert_text(self, text: str) -> None:
        """Types text as if pasted by the keyboard (handles emoji)."""
        self.cdp("Input.insertText", {"text": text})

    def screenshot(self, folder: Path, label: str) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.png"
        try:
            data = self.cdp("Page.captureScreenshot", {"format": "png"})["data"]
            p.write_bytes(base64.b64decode(data))
        except Exception as e:
            log.warning("screenshot failed: %s", e)
            return ""
        return str(p)


# ====================================================================== extension
class ExtensionBrowser(BaseBrowser):
    REUSE_EXISTING = ("whatsapp",)   # tab names that may re-use a tab you already have open

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        from .bridge import Bridge, wait_for_extension
        self.bridge = Bridge()
        wait_for_extension(self.bridge)
        self.current: int | None = None
        self.window_id: int | None = None
        self._tabs_file = Path(cfg["records"]["folder"]) / "bridge_tabs.json"

    def _call(self, cmd, timeout=90, **a):
        from .bridge import BridgeDisconnected, BridgeError
        try:
            return self.bridge.call(cmd, timeout=timeout, **a)
        except BridgeDisconnected as e:
            raise BrowserLost(str(e))
        except BridgeError as e:
            msg = str(e)
            if "No tab with id" in msg or "Cannot access" in msg:
                raise BrowserLost(msg)
            raise

    def open_tabs(self, names: list[str]) -> None:
        urls = self.cfg["urls"]
        saved = {}
        if self._tabs_file.exists():
            try:
                saved = json.loads(self._tabs_file.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
        for name in names:            # 1. tabs from the last run that still show the right site
            tid = saved.get(name)
            if tid:
                info = self._call("tabInfo", tabId=tid)
                if info.get("exists") and info["url"].startswith(urls[name].rstrip("/")):
                    self.tabs[name] = tid
                    self.window_id = self.window_id or info["windowId"]
        for name in names:            # 2. an already open WhatsApp tab
            if name not in self.tabs and name in self.REUSE_EXISTING:
                found = self._call("findTabs", prefix=urls[name].rstrip("/"))
                if found:
                    self.tabs[name] = found[0]["tabId"]
        missing = [n for n in names if n not in self.tabs]
        if missing:                   # 3. a new window for the rest
            if self.window_id:
                for n in missing:
                    self.tabs[n] = self._call("createTab", windowId=self.window_id, url=urls[n])["tabId"]
            else:
                made = self._call("openWindow", urls=[urls[n] for n in missing])
                for n, t in zip(missing, made):
                    self.tabs[n] = t["tabId"]
                    self.window_id = t["windowId"]
        self._tabs_file.write_text(json.dumps(self.tabs), encoding="utf-8")
        for n in names:
            self.tab(n)
            self.wait_for(lambda: self._call("tabInfo", tabId=self.current).get("status") == "complete", timeout=60)

    def tab(self, name: str):
        tid = self.tabs.get(name)
        info = self._call("tabInfo", tabId=tid) if tid else {"exists": False}
        if not info.get("exists"):
            log.warning("tab %s was closed – reopening", name)
            tid = self._call("createTab", windowId=self.window_id, url=self.cfg["urls"][name])["tabId"]
            self.tabs[name] = tid
            self._tabs_file.write_text(json.dumps(self.tabs), encoding="utf-8")
            time.sleep(3)
        if self.current != tid or not info.get("active"):
            self._call("activate", tabId=tid)
            time.sleep(0.3)
        self.current = tid
        return self

    def _get(self, url: str) -> None:
        self._call("navigate", tabId=self.current, url=url, timeoutMs=60000, timeout=75)

    def reload(self, name: str) -> None:
        self.tab(name)
        self._call("reload", tabId=self.current, timeoutMs=60000, timeout=75)

    def cdp(self, method: str, params: dict | None = None) -> dict:
        return self._call("cdp", tabId=self.current, method=method, params=params or {}) or {}

    def current_url(self) -> str:
        return self._call("tabInfo", tabId=self.current).get("url", "")

    def quit(self) -> None:
        # Leave your Chrome and the bot's tabs open; just stop debugging them.
        for tid in self.tabs.values():
            try:
                self.bridge.call("detach", timeout=5, tabId=tid)
            except Exception:
                pass


# ====================================================================== selenium
class SeleniumBrowser(BaseBrowser):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        ch = cfg["chrome"]
        opts = Options()
        opts.binary_location = ch["binary"]
        opts.add_argument(f"--user-data-dir={ch['user_data_dir']}")
        opts.add_argument(f"--profile-directory={ch['profile_directory']}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        self.driver = webdriver.Chrome(options=opts)
        self.driver.set_page_load_timeout(ch.get("page_load_timeout_seconds", 60))

    def _wd(self, fn, *a):
        from selenium.common.exceptions import (InvalidSessionIdException, NoSuchWindowException,
                                                WebDriverException)
        try:
            return fn(*a)
        except (InvalidSessionIdException, NoSuchWindowException) as e:
            raise BrowserLost(str(e).splitlines()[0])
        except WebDriverException as e:
            if "disconnected" in str(e) or "no such window" in str(e):
                raise BrowserLost(str(e).splitlines()[0])
            raise

    def open_tabs(self, names: list[str]) -> None:
        d = self.driver
        for i, name in enumerate(names):
            if i == 0:
                d.switch_to.window(d.window_handles[0])
            else:
                d.switch_to.new_window("tab")
            self.tabs[name] = d.current_window_handle
            self._get(self.cfg["urls"][name])

    def tab(self, name: str):
        from selenium.common.exceptions import NoSuchWindowException
        d = self.driver
        try:
            d.switch_to.window(self.tabs[name])
        except (KeyError, NoSuchWindowException):
            log.warning("tab %s was closed – reopening", name)
            d.switch_to.new_window("tab")
            self.tabs[name] = d.current_window_handle
            self._get(self.cfg["urls"][name])
        return self

    def _get(self, url: str) -> None:
        from selenium.common.exceptions import TimeoutException
        try:
            self._wd(self.driver.get, url)
        except TimeoutException:
            log.warning("loading %s timed out", url)

    def reload(self, name: str) -> None:
        from selenium.common.exceptions import TimeoutException
        self.tab(name)
        try:
            self._wd(self.driver.refresh)
        except TimeoutException:
            log.warning("refresh %s timed out", name)

    def cdp(self, method: str, params: dict | None = None) -> dict:
        return self._wd(self.driver.execute_cdp_cmd, method, params or {}) or {}

    def current_url(self) -> str:
        return self._wd(lambda: self.driver.current_url)

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass


def make_browser(cfg: dict) -> BaseBrowser:
    mode = cfg["chrome"].get("mode", "extension")
    return ExtensionBrowser(cfg) if mode == "extension" else SeleniumBrowser(cfg)


def desktop_alert(title: str, text: str) -> None:
    """Beep and show a Windows message box without blocking the bot."""
    def _show():
        try:
            ctypes.windll.user32.MessageBeep(0x30)
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40030)  # info, topmost, system modal
        except Exception:
            pass
    threading.Thread(target=_show, daemon=True).start()


def clone_profile(cfg: dict, force: bool = False) -> None:
    """Selenium mode only: copy the logged-in Chrome profile into the bot's own folder
    (all Chrome windows must be closed – the cookie file is locked while Chrome runs)."""
    ch = cfg["chrome"]
    src_root = Path(ch["source_user_data_dir"])
    dst_root = Path(ch["user_data_dir"])
    prof = ch["profile_directory"]
    if (dst_root / prof).exists() and not force:
        print(f"{dst_root / prof} already exists – use --force to copy again.")
        return
    dst_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_root / "Local State", dst_root / "Local State")
    skip = shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "CacheStorage", "DawnCache",
                                  "GrShaderCache", "ShaderCache", "*.tmp", "LOCK", "Singleton*")
    if (dst_root / prof).exists():
        shutil.rmtree(dst_root / prof)
    shutil.copytree(src_root / prof, dst_root / prof, ignore=skip, dirs_exist_ok=True)
    print(f"Copied {src_root / prof} -> {dst_root / prof}")
