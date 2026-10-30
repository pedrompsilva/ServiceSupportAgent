"""Deterministic routing from the Assets catalogue (Assets mode).

1. routing_mapping: rules whose Service and Country both match. "Global"
   in a rule's countries matches any country.
2. If no rule matches: assignment groups whose own Service and Country lists
   match.
3. Exactly one group → recommend it. None or several → human triage, with
   the candidates listed so a person can choose quickly.

The model never names an assignment group; it only names a service (and a
country when the ticket has none).
"""
from __future__ import annotations

from typing import Optional

from .catalog import Catalog
from .models import RoutingDecision


def _matches(service_ids: list[str], country_ids: list[str], service_id: str,
             country_id: Optional[str], global_id: Optional[str]) -> bool:
    if service_id not in service_ids:
        return False
    if country_id is None:
        return True
    return country_id in country_ids or (global_id is not None and global_id in country_ids)


def resolve(cat: Catalog, acfg: dict, service_id: Optional[str], country_id: Optional[str],
            acting_allowed: bool) -> RoutingDecision:
    human = RoutingDecision(target="human_triage")
    if not service_id:
        human.reason = "no Affected Service identified"
        return human

    global_id = cat.country_id(acfg.get("global_country", "Global"))
    service = cat.name_of("service", service_id)

    group_ids = list(dict.fromkeys(
        m.group_id for m in cat.mappings
        if m.group_id and _matches(m.service_ids, m.country_ids, service_id, country_id, global_id)))
    source = "routing_mapping"
    if not group_ids:
        group_ids = [g.id for g in cat.groups.values()
                     if _matches(g.service_ids, g.country_ids, service_id, country_id, global_id)]
        source = "assignment_groups"

    names = [cat.name_of("group", g) or g for g in group_ids]
    where = f"{service} / {cat.name_of('country', country_id) or 'any country'}"

    if country_id is None:
        human.candidates = names
        human.reason = f"no Country on the ticket; {len(names)} possible group(s) for {service}"
        return human
    if len(group_ids) != 1:
        human.candidates = names
        human.reason = (f"no assignment group found for {where}" if not group_ids
                        else f"{len(group_ids)} assignment groups match {where} ({source})")
        return human

    gid = group_ids[0]
    gname = names[0]
    vendors = {v.strip().lower() for v in acfg.get("vendor_groups", [])}
    decision = RoutingDecision(
        target="vendor" if gname.strip().lower() in vendors else "internal",
        assignment_group=gname, assignment_group_id=gid, source=source,
        reason=f"{where} → {gname} (from {source})",
    )
    if not acting_allowed:
        # Keep the recommendation visible, but a person decides.
        decision.target = "human_triage"
        decision.reason = f"guardrails require human review; recommended {gname} for {where}"
    return decision
