"""Excel record (one sheet per UK day + a 'Job status' sheet), a JSONL mirror, and
the bot's memory (state.json) so it never bids or posts twice."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .timeutil import now_uk

log = logging.getLogger(__name__)

EVENT_COLUMNS = [
    "Time (UK)", "Time (PC)", "Platform", "Event", "Job ref", "Pickup (UK)", "From", "To",
    "Vehicle", "Passengers", "Miles", "Guide min £", "Guide max £", "Bid £", "Decision",
    "Reason", "Email subject", "WhatsApp target", "Status", "Dry run", "Details",
]
_EVENT_KEYS = [
    "time_uk", "time_pc", "platform", "event", "ref", "pickup", "from", "to", "vehicle",
    "passengers", "miles", "guide_min", "guide_max", "bid", "decision", "reason",
    "email_subject", "wa_target", "status", "dry_run", "details",
]
STATUS_SHEET = "Job status"
STATUS_COLUMNS = [
    "Job ref", "Platform", "Status", "Pickup (UK)", "From", "To", "Vehicle", "Miles",
    "Guide", "Bid £", "Bid time (UK)", "Availability request (UK)", "Booked (UK)",
    "OTS total £", "Posted price £", "Groups posted", "Last update (UK)", "Last reason",
]

# Job status values, in order of progress.
STATUSES = ["seen", "skipped", "bid", "availability_requested", "booked", "reconfirm_requested"]


class Records:
    def __init__(self, folder: str, workbook: str, state_file: str, dry_run: bool):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.xlsx = self.folder / workbook
        self.jsonl = self.folder / "events.jsonl"
        self.state_path = self.folder / state_file
        self.dry_run = dry_run
        self._pending: list[dict] = []     # rows waiting because Excel had the file open
        self._lock = threading.Lock()
        self.state = self._load_state()

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
                # forget "already logged" marks older than 30 days so the file stays small
                cutoff = (now_uk() - timedelta(days=30)).strftime("%Y-%m-%d")
                st["logged"] = {k: v for k, v in st.get("logged", {}).items() if v >= cutoff}
                return st
            except Exception:
                log.exception("state.json unreadable – starting with empty memory (old file kept)")
                self.state_path.rename(self.state_path.with_suffix(".broken.json"))
        return {"jobs": {}, "emails": {}, "posts": {}, "alerts": {}, "logged": {}}

    def save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def job(self, ref: str) -> dict:
        return self.state["jobs"].setdefault(ref, {"ref": ref, "status": "seen"})

    def update_job(self, ref: str, **fields) -> dict:
        j = self.job(ref)
        new_status = fields.pop("status", None)
        if new_status and (j.get("status") not in STATUSES
                           or STATUSES.index(new_status) >= STATUSES.index(j["status"])
                           or j["status"] == "skipped"):
            j["status"] = new_status
        j.update({k: v for k, v in fields.items() if v not in (None, "")})
        j["last_update"] = now_uk().strftime("%Y-%m-%d %H:%M")
        self.save_state()
        return j

    def once(self, key: str) -> bool:
        """True the first time a key is seen (used to avoid logging the same skip every round)."""
        if key in self.state["logged"]:
            return False
        self.state["logged"][key] = now_uk().strftime("%Y-%m-%d %H:%M")
        return True

    # ------------------------------------------------------------ events
    def event(self, event: str, platform: str = "", **f) -> None:
        row = {
            "time_uk": now_uk().strftime("%Y-%m-%d %H:%M:%S"),
            "time_pc": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": platform, "event": event, "dry_run": "yes" if self.dry_run else "no",
        }
        row.update({k: ("" if v is None else v) for k, v in f.items()})
        log.info("%s %s %s %s", event, f.get("ref", ""), f.get("decision", ""), f.get("reason", ""))
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with self._lock:
            self._pending.append(row)
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        try:
            wb = load_workbook(self.xlsx) if self.xlsx.exists() else Workbook()
            if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1 and wb["Sheet"].max_row == 1:
                wb["Sheet"].title = STATUS_SHEET
            for row in self._pending:
                day = row["time_uk"][:10]
                ws = wb[day] if day in wb.sheetnames else self._new_day_sheet(wb, day)
                ws.append([row.get(k, "") for k in _EVENT_KEYS])
            self._write_status(wb)
            wb.save(self.xlsx)
            self._pending.clear()
        except PermissionError:
            log.warning("Excel file is open in another program – %d rows will be written later "
                        "(they are already in events.jsonl)", len(self._pending))

    @staticmethod
    def _header(ws, cols):
        ws.append(cols)
        for i, _ in enumerate(cols, start=1):
            c = ws.cell(row=1, column=i)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F3A5F")
            ws.column_dimensions[get_column_letter(i)].width = 16
        ws.freeze_panes = "A2"

    def _new_day_sheet(self, wb, day):
        ws = wb.create_sheet(day)
        self._header(ws, EVENT_COLUMNS)
        for col, w in (("G", 38), ("H", 38), ("P", 40), ("Q", 44), ("U", 60)):
            ws.column_dimensions[col].width = w
        return ws

    def _write_status(self, wb):
        if STATUS_SHEET in wb.sheetnames:
            del wb[STATUS_SHEET]
        ws = wb.create_sheet(STATUS_SHEET, 0)
        self._header(ws, STATUS_COLUMNS)
        jobs = sorted(self.state["jobs"].values(), key=lambda j: j.get("last_update", ""), reverse=True)
        for j in jobs:
            if j.get("status") in ("seen", "skipped") and not j.get("bid"):
                continue  # keep the status sheet to jobs we acted on
            ws.append([
                j.get("ref"), j.get("platform", "OTS"), j.get("status"), j.get("pickup"), j.get("from"),
                j.get("to"), j.get("vehicle"), j.get("miles"), j.get("guide"), j.get("bid"),
                j.get("bid_time"), j.get("availability_time"), j.get("booked_time"), j.get("ots_total"),
                j.get("posted_price"), len(j.get("groups_posted", [])), j.get("last_update"),
                j.get("reason"),
            ])
