"""Roles, permissions and scope (section 3, P11).

Permissions are checked in one place (``Permission`` names below); the role table maps each role to its
permissions. A ``Scope`` lists the sites and customers a user may see; every metric query takes a Scope.
A future "client portal user" role (Year 2) fits as another entry in ROLE_PERMISSIONS with a customer scope.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from . import models as m

ROLES = ("billing_analyst", "finance_controller", "account_manager", "operations_supervisor", "administrator")
ROLE_LABELS = {"billing_analyst": "Billing analyst", "finance_controller": "Finance controller", "account_manager": "Account manager",
               "operations_supervisor": "Operations supervisor", "administrator": "Administrator"}

ROLE_PERMISSIONS = {
    "billing_analyst": {"view.leakage", "view.billing", "view.customers", "view.evidence", "view.data", "view.contracts",
                        "leakage.update", "leakage.decide", "leakage.note", "manual.review", "manual.create", "rate.propose",
                        "run.start", "export.billing", "view.work_capture"},
    "finance_controller": {"view.leakage", "view.billing", "view.customers", "view.evidence", "view.data", "view.contracts", "view.costs",
                           "view.labor_detail", "view.close", "leakage.decide", "leakage.approve", "leakage.note", "rate.approve",
                           "driver.approve", "period.close", "period.reopen", "close.acknowledge", "run.start", "export.billing",
                           "export.finance", "cost.enter", "accounting.export", "view.alerts"},
    "account_manager": {"view.customers", "view.contracts.read", "export.customer"},
    "operations_supervisor": {"view.work_capture", "manual.create"},
    "administrator": {"view.admin", "view.data", "view.contracts", "admin.config", "data.import", "rate.propose", "driver.define",
                      "view.audit", "run.start"},
}


def permissions_for(roles) -> set:
    out = set()
    for r in roles:
        out |= ROLE_PERMISSIONS.get(r, set())
    return out


@dataclass(frozen=True)
class Scope:
    tenant_id: int
    site_ids: frozenset
    customer_ids_by_site: dict  # site_id -> frozenset of customer ids, or None = all customers of the site

    def site_allowed(self, site_id: int) -> bool:
        return site_id in self.site_ids

    def customers(self, site_id: int):
        """None = all customers of the site; otherwise the allowed set."""
        return self.customer_ids_by_site.get(site_id)

    def customer_allowed(self, site_id: int, customer_id: int | None) -> bool:
        if site_id not in self.site_ids:
            return False
        allowed = self.customer_ids_by_site.get(site_id)
        if allowed is None:
            return True
        return customer_id in allowed

    def filter(self, site_id: int, customer_ids):
        allowed = self.customers(site_id)
        return [c for c in customer_ids if allowed is None or c in allowed]


def load_scope(s, tenant_id: int, user_id: int) -> Scope:
    sites, by_site = set(), {}
    for x in s.execute(select(m.UserScope).where(m.UserScope.user_id == user_id)).scalars():
        sites.add(x.site_id)
        if x.customer_id is None:
            by_site[x.site_id] = None
        elif x.site_id not in by_site or by_site[x.site_id] is not None:
            by_site[x.site_id] = frozenset((by_site.get(x.site_id) or frozenset()) | {x.customer_id})
    return Scope(tenant_id, frozenset(sites), by_site)


def roles_of(s, user_id: int) -> list[str]:
    return sorted(r.role for r in s.execute(select(m.UserRole).where(m.UserRole.user_id == user_id)).scalars())
