"""G1 storage quantities from daily inventory snapshots.

Inputs: daily snapshots (customer, date, unit type, quantity, received date) for one billing period, and the
site's storage policy (setting ``storage.policy``; no default):
  {"unit_type": "pallet", "method": "average_daily" | "unit_days" | "peak",
   "output_uom": "pallet_month" | "pallet_day" | ..., "grace_days": 0, "split_month": "prorate" | "full_month"}
Method:
  * grace: units received fewer than ``grace_days`` days before the snapshot date are not counted that day;
  * unit_days = sum over days of counted quantity;
  * average_daily = unit_days / days in the period  (a "pallet-month" for a monthly period);
  * peak = the largest counted daily quantity;
  * split_month = "prorate": a customer with snapshots on only part of the period is charged for those days
    (average over the full period); "full_month": the average is taken over the days with stock.
Minimums are applied by the storage rate's ``minimum_charge`` in the normal billable step.
Output: {customer key: quantity in output_uom} (full precision; rounding happens once at the billable line).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from .money import ZERO, D


def storage_quantities(snapshots, start: dt.date, end: dt.date, policy: dict) -> dict:
    unit = policy["unit_type"]
    method = policy["method"]
    grace = int(policy.get("grace_days", 0))
    split = policy.get("split_month", "prorate")
    days_total = (end - start).days + 1
    daily = defaultdict(lambda: defaultdict(lambda: ZERO))
    for sn in snapshots:
        if sn["unit_type"] != unit or not (start <= sn["date"] <= end):
            continue
        rd = sn.get("received_date")
        if grace and rd is not None and (sn["date"] - rd).days < grace:
            continue
        daily[sn["customer"]][sn["date"]] += D(sn["quantity"])
    out = {}
    for cust, by_day in daily.items():
        unit_days = sum(by_day.values(), ZERO)
        if method == "unit_days":
            out[cust] = unit_days
        elif method == "peak":
            out[cust] = max(by_day.values())
        elif method == "average_daily":
            denom = days_total if split == "prorate" else len(by_day)
            out[cust] = unit_days / Decimal(denom)
        else:
            raise ValueError(f"unknown storage method {method}")
    return out
