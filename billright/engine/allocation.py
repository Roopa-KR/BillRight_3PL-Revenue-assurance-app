"""Cost allocation (7.6).

Inputs: the period's expense lines, approved allocation drivers per expense category and their driver
quantities per customer (a quantity with customer ``None`` is unassigned activity such as idle or shared time).
Method:
 1. Direct costs first: an expense line with a customer is assigned 100% to that customer.
 2. Remaining lines form one pool per expense category. The pool is split with the approved driver:
    share_c = driver_quantity_c / sum(driver quantities); allocated_c = pool x share_c.
 3. Cents are split with largest remainder so the parts add up exactly to the pool.
 4. Reconciliation per pool: pool = sum allocated to customers + unassigned.
 5. A pool with no approved driver, more than one approved driver, or a zero driver total is left fully
    unassigned and reported as an exception. It is never split equally.
Output: allocation rows and pool summaries with their exceptions.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, D, largest_remainder

EXPENSE_CATEGORIES = (
    "warehouse_operators", "forklift_operators", "supervisors", "managers", "administration", "transport_carriers",
    "warehouse_rent", "utilities", "equipment", "packaging", "software", "other_overhead",
)
PAYMENT_RECIPIENTS = ("direct_payroll", "staffing_agency", "carrier", "facility_equipment_it_other")
DRIVER_TYPES = (
    "paid_labor_hours_by_customer", "documented_time_share", "equipment_hours_by_customer", "freight_bill_by_customer",
    "occupied_space_days", "pallet_days", "reserved_space", "metered_usage", "materials_consumed",
    "transactions_by_customer", "completed_line_share_proxy", "direct",
)


@dataclass(frozen=True)
class ExpenseSpec:
    id: int
    category: str
    amount: Decimal
    customer_id: int | None
    payment_recipient_type: str
    description: str = ""


@dataclass(frozen=True)
class DriverSpec:
    id: int
    category: str
    driver_type: str
    is_proxy: bool = False
    description: str = ""


@dataclass
class AllocationRow:
    category: str
    expense_line_id: int | None
    customer_id: int | None
    amount: Decimal
    method: str  # direct | driver | none
    driver_id: int | None
    share: Decimal | None
    exception: str | None = None


@dataclass
class PoolResult:
    category: str
    pool_amount: Decimal  # all lines of the category (direct + shared)
    direct_amount: Decimal
    shared_amount: Decimal
    allocated_amount: Decimal  # to customers (direct + driver)
    unassigned_amount: Decimal
    driver: DriverSpec | None
    exception: str | None
    rows: list = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        return self.pool_amount == self.allocated_amount + self.unassigned_amount


def allocate(expenses: list[ExpenseSpec], drivers: dict, quantities: dict) -> dict:
    """``drivers[category]`` = list of approved DriverSpec covering the period;
    ``quantities[driver_id]`` = {customer_id or None: Decimal}."""
    by_cat = defaultdict(list)
    for e in expenses:
        by_cat[e.category].append(e)
    pools = {}
    for cat in sorted(by_cat, key=lambda c: (EXPENSE_CATEGORIES.index(c) if c in EXPENSE_CATEGORIES else 99, c)):
        lines = sorted(by_cat[cat], key=lambda e: e.id)
        rows = []
        direct = ZERO
        for e in lines:
            if e.customer_id is not None:
                rows.append(AllocationRow(cat, e.id, e.customer_id, D(e.amount), "direct", None, Decimal(1)))
                direct += D(e.amount)
        shared = sum((D(e.amount) for e in lines if e.customer_id is None), ZERO)
        total = direct + shared
        exception = None
        driver = None
        allocated_shared = ZERO
        unassigned = ZERO
        if shared != 0:
            cands = drivers.get(cat, [])
            if not cands:
                exception = "no_approved_driver"
            elif len(cands) > 1:
                exception = "more_than_one_approved_driver"
            else:
                driver = cands[0]
                q = {k: D(v) for k, v in quantities.get(driver.id, {}).items() if D(v) != 0}
                if driver.driver_type == "direct":
                    exception = "direct_only_category_has_unattributed_lines"
                elif not q or sum(q.values(), ZERO) == 0:
                    exception = "zero_driver_quantity"
                else:
                    keys = sorted((k for k in q if k is not None)) + ([None] if None in q else [])
                    parts = largest_remainder(shared, {k: q[k] for k in keys})
                    qsum = sum(q.values(), ZERO)
                    for k in keys:
                        share = (q[k] / qsum).quantize(Decimal("0.00000001"))
                        if k is None:
                            unassigned += parts[k]
                            rows.append(AllocationRow(cat, None, None, parts[k], "driver", driver.id, share, "unassigned_activity"))
                        else:
                            allocated_shared += parts[k]
                            rows.append(AllocationRow(cat, None, k, parts[k], "driver", driver.id, share))
            if exception:
                unassigned = shared
                rows.append(AllocationRow(cat, None, None, shared, "none", driver.id if driver else None, None, exception))
        pools[cat] = PoolResult(cat, total, direct, shared, direct + allocated_shared, unassigned, driver, exception, rows)
    return pools
