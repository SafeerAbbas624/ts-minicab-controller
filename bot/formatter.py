"""Build the WhatsApp texts from config templates."""
from __future__ import annotations

from datetime import datetime, time as dtime

from .timeutil import UK


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def day_word(pickup: datetime, now: datetime, pcfg: dict) -> str:
    p, n = pickup.astimezone(UK), now.astimezone(UK)
    days = (p.date() - n.date()).days
    if days == 0:
        if p.time() < _hm(pcfg.get("morning_until", "12:00")):
            return "TODAY MORNING"
        if p.time() >= _hm(pcfg.get("tonight_from", "18:00")):
            return "TONIGHT"
        return "TODAY"
    if days == 1:
        return "TOMORROW"
    return p.strftime("%A").upper()


def _money(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 0.005 else f"{x:.2f}"


def _clean(text: str) -> str:
    # Drop lines that are empty after filling (e.g. no miles), keep single blank lines.
    out, blank = [], False
    for line in text.splitlines():
        if line.strip():
            out.append(line.rstrip())
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def booking_post(fields: dict, miles: float | None, now: datetime, pcfg: dict, ref: str) -> tuple[str, float | None]:
    start: datetime | None = fields.get("starting")
    total = fields.get("total") or fields.get("quoted")
    price = round(total - pcfg["price_minus"], 2) if total is not None else None
    miles_line = ""
    if pcfg.get("show_miles", True) and miles is not None:
        miles_line = pcfg.get("miles_line", "ABOUT {miles} MILES").format(miles=f"{miles:g}")
    text = pcfg["template"].format(
        day_word=day_word(start, now, pcfg) if start else "",
        date=start.astimezone(UK).strftime("%d%b").upper().lstrip("0") if start else "",
        time=start.astimezone(UK).strftime("%I:%M %p").lstrip("0") if start else fields.get("starting_text", ""),
        pickup=fields.get("pickup", ""), dropoff=fields.get("dropoff", ""),
        vehicle=fields.get("vehicle", "") if pcfg.get("vehicle_source", "ots") == "ots" else "Any Car",
        price=_money(price) if price is not None else "?",
        miles_line=miles_line, payment_line=pcfg.get("payment_line", ""), ref=ref,
    )
    return _clean(text), price


def fields_from_job(job: dict, price: float | None) -> dict | None:
    """Post fields from what the bot saved when it bid (the Availability email has no
    addresses or time). None if the bot never saw this job."""
    try:
        start = datetime.strptime(job.get("pickup", ""), "%Y-%m-%d %H:%M").replace(tzinfo=UK)
    except ValueError:
        return None
    if not (job.get("from") and job.get("to")) or price is None:
        return None
    return {"starting": start, "pickup": job["from"], "dropoff": job["to"],
            "vehicle": job.get("vehicle", ""), "total": price}


def availability_alert(ref: str, fields: dict, pcfg: dict) -> str:
    exp = fields.get("expires")
    return _clean(pcfg["availability_alert_template"].format(
        ref=ref, quoted=_money(fields["quoted"]) if fields.get("quoted") else "?",
        expires=exp.astimezone(UK).strftime("%H:%M %a %d %b") if exp else fields.get("expires_text", "?")))


def reconfirmation_alert(ref: str, fields: dict, pcfg: dict) -> str:
    st = fields.get("starting")
    return _clean(pcfg["reconfirmation_alert_template"].format(
        ref=ref, changes=fields.get("changes") or "details", pickup=fields.get("pickup", ""),
        dropoff=fields.get("dropoff", ""), when=st.strftime("%a %d %b %H:%M") if st else fields.get("starting_text", ""),
        total=_money(fields["total"]) if fields.get("total") else "?"))
