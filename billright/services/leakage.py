"""Leakage review workflow (F7, 8.3).

Statuses: new -> under_review -> decided -> approved (only when |gap| is above the approval threshold).
Runs may also set ``resolved_by_data``. Decisions (exactly one):
  under-billing: bill_now, bill_next_cycle, waive (reason category required), not_billable (reason required)
  over-billing:  issue_credit, not_owed (reason required), waive (reason required)
Every decision stores person, time and reason. 'Bill now' is refused when the period ended more than the
back-billing window ago. The person who decides an item above the threshold cannot approve it.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from .. import models as m
from .common import Actor, BusinessError, audit, ensure_open, get_setting, row_dict

UNDER_DECISIONS = ("bill_now", "bill_next_cycle", "waive", "not_billable")
OVER_DECISIONS = ("issue_credit", "not_owed", "waive")
DECISION_LABELS = {"bill_now": "Bill now", "bill_next_cycle": "Bill next cycle", "waive": "Waive", "not_billable": "Not billable",
                   "issue_credit": "Issue credit", "not_owed": "Not owed"}
WAIVE_REASONS = ("commercial_goodwill", "contract_interpretation", "below_materiality", "other")
STATUS_LABELS = {"new": "New", "under_review": "Under review", "decided": "Decided", "approved": "Approved", "resolved_by_data": "Resolved by data"}
CONFIRMED = ("bill_now", "bill_next_cycle")


def allowed_decisions(item) -> tuple:
    return OVER_DECISIONS if item.direction == "over" else UNDER_DECISIONS


def _item(s, item_id) -> m.LeakageItem:
    it = s.get(m.LeakageItem, item_id)
    if it is None:
        raise BusinessError("Leakage item not found")
    return it


def needs_approval(s, actor: Actor, item) -> bool:
    threshold = get_setting(s, actor.tenant_id, "leakage.approval_threshold", item.site_id)
    if threshold is None:
        raise BusinessError("Fill the setting 'leakage.approval_threshold' before deciding items.")
    return abs(Decimal(item.gap_amount)) > threshold


def assign(s, actor: Actor, item_ids, owner_id):
    for iid in item_ids:
        it = _item(s, iid)
        before = {"owner_id": it.owner_id, "status": it.status}
        it.owner_id = owner_id
        if it.status == "new":
            it.status = "under_review"
        audit(s, actor, "leakage.assign", "leakage_item", it.id, before, {"owner_id": owner_id, "status": it.status})


def start_review(s, actor: Actor, item_id):
    it = _item(s, item_id)
    if it.status == "new":
        audit(s, actor, "leakage.review", "leakage_item", it.id, {"status": "new"}, {"status": "under_review"})
        it.status = "under_review"
        if it.owner_id is None:
            it.owner_id = actor.user_id


def add_note(s, actor: Actor, item_id, text, attachment_ref=None):
    if not text or not text.strip():
        raise BusinessError("The note is empty.")
    it = _item(s, item_id)
    n = m.LeakageNote(tenant_id=actor.tenant_id, leakage_item_id=it.id, author=actor.user_id, text=text.strip(), attachment_ref=attachment_ref)
    s.add(n)
    s.flush()
    audit(s, actor, "leakage.note", "leakage_item", it.id, None, {"note_id": n.id, "text": n.text})
    return n


def decide(s, actor: Actor, item_id, decision: str, reason_category: str | None = None, reason_text: str | None = None, today: dt.date | None = None):
    it = _item(s, item_id)
    period = s.get(m.BillingPeriod, it.period_id)
    ensure_open(period, "Deciding this item")
    if it.status in ("approved", "resolved_by_data"):
        raise BusinessError(f"The item is {STATUS_LABELS[it.status].lower()}; it cannot be decided again.")
    if decision not in allowed_decisions(it):
        raise BusinessError(f"Choose one of: {', '.join(DECISION_LABELS[d] for d in allowed_decisions(it))}.")
    reason_text = (reason_text or "").strip()
    if decision == "waive":
        if reason_category not in WAIVE_REASONS:
            raise BusinessError("Choose a waive reason: commercial goodwill, contract interpretation, below materiality or other.")
        if reason_category == "other" and not reason_text:
            raise BusinessError("Describe the reason for 'Other'.")
    if decision in ("not_billable", "not_owed") and not reason_text:
        raise BusinessError("A reason is required.")
    if decision == "bill_now":
        window = get_setting(s, actor.tenant_id, "leakage.back_billing_window_days", it.site_id)
        if window is None:
            raise BusinessError("Fill the setting 'leakage.back_billing_window_days' first.")
        age = ((today or dt.date.today()) - period.end_date).days
        if age > window:
            raise BusinessError(f"'Bill now' is not allowed: {period.label} ended {age} days ago, beyond the {window}-day back-billing window.")
    before = row_dict(it, ["status", "decision", "decision_reason", "decided_by", "approved_by"])
    it.decision = decision
    it.decision_reason = ": ".join(x for x in (reason_category, reason_text) if x) or None
    it.decided_by = actor.user_id
    it.decided_at = dt.datetime.now(dt.timezone.utc)
    it.approved_by = None
    it.approved_at = None
    it.status = "decided"
    audit(s, actor, "leakage.decide", "leakage_item", it.id, before,
          row_dict(it, ["status", "decision", "decision_reason", "decided_by"]), it.decision_reason)
    return it


def approve(s, actor: Actor, item_id):
    it = _item(s, item_id)
    ensure_open(s.get(m.BillingPeriod, it.period_id), "Approving this item")
    if it.status != "decided":
        raise BusinessError("Only a decided item can be approved.")
    if not needs_approval(s, actor, it):
        raise BusinessError("This item is below the approval threshold and needs no approval.")
    if it.decided_by == actor.user_id:
        raise BusinessError("Segregation of duties: the person who decided this item cannot approve it.")
    it.status = "approved"
    it.approved_by = actor.user_id
    it.approved_at = dt.datetime.now(dt.timezone.utc)
    audit(s, actor, "leakage.approve", "leakage_item", it.id, {"status": "decided"}, {"status": "approved"})
    return it


def send_back(s, actor: Actor, item_id, reason: str):
    it = _item(s, item_id)
    if it.status != "decided":
        raise BusinessError("Only a decided item can be sent back.")
    if not reason:
        raise BusinessError("A reason is required.")
    if it.decided_by == actor.user_id:
        raise BusinessError("The person who decided the item cannot also be its approver.")
    audit(s, actor, "leakage.send_back", "leakage_item", it.id, {"status": "decided", "decision": it.decision},
          {"status": "under_review"}, reason)
    it.status = "under_review"
    it.decision = None
    it.decided_by = None
    it.decided_at = None


def is_final(s, actor, it) -> bool:
    """A decision takes effect when it is decided and either below the threshold or approved."""
    if it.status == "approved":
        return True
    if it.status != "decided":
        return False
    return not needs_approval(s, actor, it)
