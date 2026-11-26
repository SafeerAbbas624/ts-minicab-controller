"""TS Minicab controller bot – start here.

  python run_bot.py                 # uses dry_run from config.yaml (true by default)
  python run_bot.py --dry-run       # never bids, never posts – just logs what it would do
  python run_bot.py --live          # REAL bids and REAL WhatsApp posts (asks you to type LIVE)
  python run_bot.py --once          # one round, then stop
  python run_bot.py --live --minutes 60   # live for one hour, then stop
  python run_bot.py --only bid      # only the OTS bidding part (or: --only email)
  python run_bot.py --list-chats    # print every WhatsApp chat name (to check config groups)
  python run_bot.py --install-extension  # once: make the bridge token, then load the extension in Chrome
  python run_bot.py --setup-profile # selenium mode only: copy the Chrome profile (close Chrome first)
  python run_bot.py --selftest      # offline checks of the parsers, rules and message format
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bot.config import load_config  # noqa: E402


def setup_logging(folder: str) -> None:
    Path(folder).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(Path(folder) / "bot.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    from bot.console import ConsoleFormatter, enable_colours
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(ConsoleFormatter(enable_colours()))
    root.addHandler(sh)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="TS Minicab controller bot")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--yes", action="store_true", help="with --live: skip the 'type LIVE' question")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, help="stop after this many minutes (e.g. a 60-minute test)")
    ap.add_argument("--only", choices=["bid", "email"])
    ap.add_argument("--list-chats", action="store_true")
    ap.add_argument("--setup-profile", action="store_true")
    ap.add_argument("--install-extension", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        from tests.selftest import main as selftest
        return selftest()

    if not Path(a.config).exists():
        raise SystemExit(f"{a.config} not found. Copy config.example.yaml to config.yaml and fill in "
                         f"your WhatsApp groups and alert numbers (see README).")
    cfg = load_config(a.config)
    setup_logging(cfg["records"]["folder"])

    if a.install_extension:
        from bot.bridge import EXT_DIR, install_token
        install_token(cfg["chrome"].get("bridge_port", 8765))
        print(f"""Bridge token written to {EXT_DIR / 'config.js'}.

Now, in Chrome with the logged-in profile:
  1. open  chrome://extensions
  2. switch on "Developer mode" (top right)
  3. click "Load unpacked" and choose:  {EXT_DIR}
     (already loaded? click its reload arrow instead)
  4. pin it: its badge shows 'on' while the bot is running.
Then start the bot:  python run_bot.py --dry-run --once""")
        return 0

    if a.setup_profile:
        from bot.browser import clone_profile
        clone_profile(cfg, force=a.force)
        return 0

    if a.list_chats:
        from bot.browser import make_browser
        from bot.whatsapp import WhatsApp
        b = make_browser(cfg)
        b.open_tabs(["whatsapp"])
        wa = WhatsApp(b, cfg)
        b.wait_for(lambda: wa.login_state() == "in", timeout=120)
        names = wa.list_groups()
        configured = set(cfg["whatsapp"]["groups"])
        for n in names:
            print(("[in config] " if n in configured else "            ") + n)
        missing = configured - set(names)
        if missing:
            print("\nIn config but NOT found in WhatsApp:", *sorted(missing), sep="\n  ")
        b.quit()
        return 0

    dry_run = True if a.dry_run else False if a.live else bool(cfg.get("dry_run", True))
    if not dry_run and not a.yes:
        print("LIVE MODE: the bot will place REAL bids on OTS and send REAL WhatsApp posts/alerts.")
        if input("Type LIVE to continue: ").strip() != "LIVE":
            print("Not started.")
            return 1
    logging.getLogger(__name__).info("starting – %s mode", "DRY-RUN" if dry_run else "LIVE")

    from bot.controller import Controller
    c = Controller(cfg, dry_run=dry_run, only=a.only)
    try:
        c.run_forever(once=a.once, minutes=a.minutes)
    except KeyboardInterrupt:
        c.records.event("bot_stopped", details="Ctrl+C")
    finally:
        c.records.flush()
        if c.browser and a.once:
            c.browser.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
