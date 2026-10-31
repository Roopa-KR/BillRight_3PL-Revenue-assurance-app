"""Labor attribution (7.7).

Inputs: per worker and period the imported paid hours and loaded cost (EMPLOYEE_LABOR_COST) and the
attributed hours per customer (LABOR_TIME_ATTRIBUTION; customer ``None`` = training, shared work, idle or
meetings, with a reason).
Method:
  loaded rate_worker = loaded_cost / paid_hours
  assigned labor cost_{worker,customer} = attributed hours_{worker,customer} x loaded rate_worker
  Paid hours with no attribution row are unassigned with reason ``not_attributed``.
  Hours attributed beyond paid hours are an error (reported, never scaled silently).
  Cents per worker are split with largest remainder so each worker's parts add up to the loaded cost.
  Unassigned hours stay unassigned; they are never spread by completed picks.
Proxy (``completed_line_share_proxy``, only when task-time attribution is unavailable):
  Labor_{c,a} = Labor cost_a x completed lines_{c,a} / completed lines_a; labelled "Proxy: completed-line share".
Output: per worker rows (customer or unassigned, hours, cost) and coverage = attributed cost / total labor cost.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .money import ZERO, D, largest_remainder


@dataclass(frozen=True)
class WorkerCost:
    worker_id: str
    employer_type: str
    role: str
    paid_hours: Decimal
    loaded_cost: Decimal


@dataclass(frozen=True)
class Attribution:
    worker_id: str
    customer_id: int | None
    hours: Decimal
    basis: str
    unassigned_reason: str | None = None


@dataclass
class LaborRow:
    worker_id: str
    customer_id: int | None
    hours: Decimal
    loaded_rate: Decimal
    cost: Decimal
    basis: str
    unassigned_reason: str | None


def loaded_rate(w: WorkerCost) -> Decimal | None:
    return None if D(w.paid_hours) == 0 else D(w.loaded_cost) / D(w.paid_hours)


def attribute(workers: list[WorkerCost], attributions: list[Attribution]):
    by_worker = defaultdict(list)
    for a in attributions:
        by_worker[a.worker_id].append(a)
    rows, errors = [], []
    for w in sorted(workers, key=lambda x: x.worker_id):
        rate = loaded_rate(w)
        if rate is None:
            errors.append({"worker_id": w.worker_id, "error": "paid hours are zero; loaded rate not available"})
            continue
        parts = defaultdict(lambda: ZERO)
        meta = {}
        for a in by_worker.get(w.worker_id, []):
            key = (a.customer_id, a.unassigned_reason if a.customer_id is None else None)
            parts[key] += D(a.hours)
            meta[key] = a.basis
        attributed = sum(parts.values(), ZERO)
        if attributed > D(w.paid_hours):
            errors.append({"worker_id": w.worker_id, "error": f"attributed hours {attributed} exceed paid hours {w.paid_hours}"})
            continue
        if attributed < D(w.paid_hours):
            key = (None, "not_attributed")
            parts[key] += D(w.paid_hours) - attributed
            meta[key] = "documented_time"
        keys = sorted(parts, key=lambda k: (k[0] is None, k[0] or 0, k[1] or ""))
        cents = largest_remainder(D(w.loaded_cost), {k: parts[k] for k in keys})
        for k in keys:
            rows.append(LaborRow(w.worker_id, k[0], parts[k], rate, cents[k], meta[k], k[1]))
    return rows, errors


def coverage(rows: list[LaborRow]) -> Decimal | None:
    total = sum((r.cost for r in rows), ZERO)
    if total == 0:
        return None
    return sum((r.cost for r in rows if r.customer_id is not None), ZERO) / total


def completed_line_share_proxy(labor_cost_by_activity: dict, lines: dict) -> dict:
    """Labor_{c,a} = cost_a x lines_{c,a} / lines_a. ``lines[(customer, activity)]`` = completed lines.
    Returns {(customer, activity): cost} with exact-cent split per activity."""
    out = {}
    for act, cost in sorted(labor_cost_by_activity.items()):
        weights = {c: n for (c, a), n in sorted(lines.items(), key=lambda kv: str(kv[0])) if a == act and n}
        if not weights:
            continue
        for c, amt in largest_remainder(D(cost), weights).items():
            out[(c, act)] = amt
    return out
