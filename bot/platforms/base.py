"""Plug-in contract for bidding platforms.

To add a platform (e.g. another transfer marketplace):
  1. create bot/platforms/<name>.py with a class that subclasses BiddingPlatform;
  2. register it in PLATFORMS in bot/platforms/__init__.py;
  3. add its URL under `urls:` and its rules under `bidding:<name>:` in config.yaml.
The controller then logs in-checks, lists jobs, applies the shared rules and bids
through the same methods for every platform.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    platform: str
    ref: str
    pickup: datetime | None = None       # aware, Europe/London
    pickup_text: str = ""
    origin: str = ""
    destination: str = ""
    vehicle: str = ""
    passengers: str = ""
    miles: float | None = None
    guide_min: float | None = None
    guide_max: float | None = None
    expires: datetime | None = None
    notes: str = ""
    details_loaded: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class BidResult:
    ok: bool
    message: str = ""
    screenshot: str = ""


class BiddingPlatform(ABC):
    name = "base"

    def __init__(self, browser, cfg: dict, records):
        self.browser = browser
        self.cfg = cfg
        self.rules = cfg["bidding"][self.name]
        self.records = records

    @property
    def tab_name(self) -> str:
        return self.name

    @abstractmethod
    def login_state(self) -> str:
        """'in', 'out' or 'loading'."""

    @abstractmethod
    def refresh(self) -> None:
        """Reload the job list."""

    @abstractmethod
    def list_jobs(self) -> list[Job]:
        """Jobs from the list view (cheap: ref, pickup, route, vehicle)."""

    @abstractmethod
    def load_details(self, job: Job) -> Job:
        """Open the job and fill miles / price guide / passengers."""

    @abstractmethod
    def place_bid(self, job: Job, amount: float, dry_run: bool) -> BidResult:
        """Type the amount and submit. Must do nothing real when dry_run is True."""

    def cleanup(self, job: Job) -> None:
        """Optional: close the job view."""
