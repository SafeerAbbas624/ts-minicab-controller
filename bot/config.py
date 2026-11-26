"""Load and sanity-check config.yaml."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REQUIRED = ["chrome", "urls", "bidding", "email", "whatsapp", "post", "records"]


def load_config(path: str | Path) -> dict:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise SystemExit(f"config.yaml is missing sections: {', '.join(missing)}")
    for key in ("booking_subjects", "reconfirmation_subjects", "availability_subjects"):
        for pat in cfg["email"].get(key, []):
            rx = re.compile(pat)
            if rx.groups < 1:
                raise SystemExit(f"email.{key}: pattern {pat!r} needs a (...) group for the OTS reference")
    for c in cfg["whatsapp"].get("alert_contacts", []):
        if not re.fullmatch(r"\d{8,15}", str(c.get("phone", ""))):
            raise SystemExit(f"whatsapp.alert_contacts: phone for {c.get('name')!r} must be digits only")
    # Relative folders are relative to the bot folder, so the whole folder can be moved/copied.
    base = path.resolve().parent
    for sec, key in (("records", "folder"), ("chrome", "user_data_dir")):
        v = cfg.get(sec, {}).get(key)
        if v and not Path(v).is_absolute():
            cfg[sec][key] = str(base / v)
    cfg.setdefault("dry_run", True)
    cfg.setdefault("loop_interval_seconds", 120)
    Path(cfg["records"]["folder"]).mkdir(parents=True, exist_ok=True)
    return cfg
