"""The bidding rules, shared by every platform. Pure functions – easy to test."""
from __future__ import annotations

from datetime import datetime

from .platforms.base import Job


def precheck(job: Job, rules: dict, now: datetime) -> str | None:
    """Reasons to skip using only the list row (saves opening the job). None = keep going."""
    skip_v = [v.lower() for v in rules.get("skip_vehicles") or []]
    if job.vehicle and job.vehicle.lower() in skip_v:
        return f"vehicle '{job.vehicle}' is in skip_vehicles"
    if job.pickup is None:
        return None  # the detail page may still have it
    hours = (job.pickup - now).total_seconds() / 3600
    if hours < rules["min_hours_before_pickup"]:
        return f"pickup in {hours:.1f} h (< {rules['min_hours_before_pickup']} h)"
    if job.expires and job.expires <= now:
        return "tender already expired"
    return None


def decide(job: Job, rules: dict, now: datetime) -> tuple[str, str, float | None]:
    """Returns (decision, reason, bid). decision is 'bid' or 'skip'."""
    if job.pickup is None:
        return "skip", f"could not read pickup time ({job.pickup_text!r})", None
    hours = (job.pickup - now).total_seconds() / 3600
    if hours < rules["min_hours_before_pickup"]:
        return "skip", f"pickup in {hours:.1f} h (< {rules['min_hours_before_pickup']} h)", None
    if job.expires and job.expires <= now:
        return "skip", "tender already expired", None
    if job.miles is None:
        if rules.get("skip_if_no_miles", True):
            return "skip", "no distance shown", None
    elif job.miles > rules["max_miles"]:
        return "skip", f"{job.miles:g} miles (> {rules['max_miles']})", None
    if job.guide_max is None:
        return "skip", "no 'expecting prices between £X and £Y' on the job", None
    adj = bid_adjustment(rules)
    bid = round(job.guide_max + adj, 2)
    if bid < rules.get("min_bid", 0):
        return "skip", f"bid £{bid:.2f} below safety minimum £{rules.get('min_bid')}", None
    if job.extra.get("has_fare_box") is False:
        return "skip", "no price box (already quoted?)", None
    miles = f"{job.miles:g} mi" if job.miles is not None else "miles n/a"
    return "bid", (f"pickup in {hours:.1f} h, {miles}, guide £{job.guide_min:.2f}-£{job.guide_max:.2f} "
                   f"-> £{job.guide_max:.2f} {describe_adjustment(rules)}"), bid


def bid_adjustment(rules: dict) -> float:
    """Pounds added to the top of OTS's price range: `bid_adjust: +15` = £15 above,
    `bid_adjust: -3` = £3 below. Older configs: `bid_below_max_guide: 3` = £3 below."""
    if rules.get("bid_adjust") is not None:
        return float(rules["bid_adjust"])
    return -float(rules.get("bid_below_max_guide", 0))


def describe_adjustment(rules: dict) -> str:
    adj = bid_adjustment(rules)
    return f"{'+' if adj >= 0 else '-'} {abs(adj):g}"
