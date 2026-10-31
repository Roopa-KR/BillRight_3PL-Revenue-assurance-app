"""Customer resolution (7.1, 5.2).

Inputs: activity events, the tenant's configured join path per event type, indexes over the active source
snapshots, and ``customer_identity_map`` entries.
Method: start from the event field named by the join path (for example ``task_ref``), follow each step
(table, match column, take column) through the source snapshots, and finish with an identity-map lookup
(source key type -> customer, valid on the service date). CLIENTID is never used: it identifies the GiNi
tenant, not the 3PL customer.
  exactly one customer -> resolved
  none                -> unresolved, reason ``no_match`` (with the step where the chain broke)
  more than one       -> unresolved, reason ``more_than_one_customer``
Events whose source already names the customer through an identity key (``attributes.customer_key``)
use the identity map directly.
Output: {event index: customer_id} and a list of unresolved records with reasons. Nothing is guessed.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Unresolved:
    index: int
    reason: str
    step: str
    values: list


def start_values(event, field: str):
    if field.startswith("attribute:"):
        v = (event.attributes or {}).get(field.split(":", 1)[1])
    else:
        v = getattr(event, field, None)
    return {str(v)} if v not in (None, "") else set()


def identity_lookup(identity: dict, key_type: str, value: str, day: dt.date):
    out = set()
    for customer_id, valid_from, valid_to in identity.get((key_type, value), []):
        if valid_from <= day and (valid_to is None or day <= valid_to):
            out.add(customer_id)
    return out


def resolve(events, join_paths: dict, indexes: dict, identity: dict):
    """``indexes[(table, match, take)]`` = {match value: set(take values)};
    ``identity[(key_type, value)]`` = [(customer_id, valid_from, valid_to)]."""
    resolved, unresolved = {}, []
    for i, ev in enumerate(events):
        key = (ev.attributes or {}).get("customer_key")
        key_type = (ev.attributes or {}).get("customer_key_type")
        if key and key_type:
            found = identity_lookup(identity, key_type, str(key), ev.service_date)
            if len(found) == 1:
                resolved[i] = next(iter(found))
            else:
                unresolved.append(Unresolved(i, "no_match" if not found else "more_than_one_customer", f"identity_map:{key_type}", sorted(found)))
            continue
        path = join_paths.get(ev.event_type)
        if not path:
            unresolved.append(Unresolved(i, "no_join_path", ev.event_type, []))
            continue
        values = start_values(ev, path[0]["from"])
        step_name = path[0]["from"]
        customers = set()
        for step in path[1:]:
            if not values:
                break
            if "identity_map" in step:
                step_name = f"identity_map:{step['identity_map']}"
                for v in values:
                    customers |= identity_lookup(identity, step["identity_map"], v, ev.service_date)
                values = set()
                break
            step_name = f"{step['table']}.{step['match']}->{step['take']}"
            index = indexes.get((step["table"], step["match"], step["take"]), {})
            nxt = set()
            for v in values:
                nxt |= index.get(v, set())
            values = nxt
        if len(customers) == 1:
            resolved[i] = next(iter(customers))
        elif not customers:
            unresolved.append(Unresolved(i, "no_match", step_name, []))
        else:
            unresolved.append(Unresolved(i, "more_than_one_customer", step_name, sorted(customers)))
    return resolved, unresolved
