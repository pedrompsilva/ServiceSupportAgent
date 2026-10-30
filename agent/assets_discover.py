"""Read-only discovery of how ITSM routing is modelled in Jira Assets.

Produces a report of:
  * Jira custom fields that look like the Affected service / routing fields
  * (optional) the raw value of those fields on one real ticket
  * every Assets schema and object type, with object counts
  * for the relevant object types: their attributes and a few sample objects

Nothing is written anywhere except the local JSON report. Sample values are
redacted (e-mails, phone numbers, long numbers) unless redact=False.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import sanitize
from .assets_client import AssetsClient, AssetsError, describe_attribute, object_attributes

KEYWORDS = ("service", "rout", "assign", "vendor", "supplier", "team", "group", "owner", "support")
FIELD_PATTERN = re.compile(r"service|asset|rout|assign|vendor|supplier|team|group", re.IGNORECASE)
MAX_DETAILED_TYPES = 15


def _err(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, AssetsError) and exc.status in (401, 403):
        msg += "  ← check that your account can view this Assets schema (object viewer role)"
    return msg


def _redact(value: Any, redact: bool) -> Any:
    if not redact:
        return value
    if isinstance(value, str):
        return sanitize.redact(value)
    if isinstance(value, list):
        return [_redact(v, redact) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, redact) for k, v in value.items()}
    return value


def _wanted(name: str, filters: Optional[list[str]]) -> bool:
    n = (name or "").lower()
    if filters:
        return any(f.lower() in n for f in filters)
    return any(k in n for k in KEYWORDS)


def discover(assets: AssetsClient, *, ticket: Optional[str] = None, type_filter: Optional[list[str]] = None,
             schema_filter: Optional[list[str]] = None, samples: int = 3, redact: bool = True) -> dict:
    report: dict[str, Any] = {"errors": []}

    # 1. Jira fields ---------------------------------------------------------
    try:
        fields = assets.jira_get("/rest/api/3/field") or []
        report["jira_fields"] = [
            {"id": f.get("id"), "name": f.get("name"),
             "custom_type": (f.get("schema") or {}).get("custom", ""),
             "type": (f.get("schema") or {}).get("type", "")}
            for f in fields
            if f.get("custom") and (FIELD_PATTERN.search(f.get("name") or "")
                                    or "cmdb" in ((f.get("schema") or {}).get("custom") or ""))
        ]
    except Exception as exc:
        report["jira_fields"] = []
        report["errors"].append(f"Jira fields: {_err(exc)}")

    # 2. One ticket's values for those fields --------------------------------
    if ticket:
        ids = [f["id"] for f in report["jira_fields"]]
        try:
            data = assets.jira_get(f"/rest/api/3/issue/{ticket}",
                                   params={"fields": ",".join(ids + ["summary"])}) or {}
            values = {k: v for k, v in (data.get("fields") or {}).items() if v not in (None, [], "")}
            report["ticket"] = {"key": ticket, "fields": _redact(values, redact)}
        except Exception as exc:
            report["errors"].append(f"ticket {ticket}: {_err(exc)}")

    # 3. Assets schemas and object types -------------------------------------
    try:
        report["workspace_id"] = assets.workspace_id
    except Exception as exc:
        report["errors"].append(f"Assets workspace: {_err(exc)}")
        report["schemas"] = []
        return report

    try:
        schemas = assets.schemas()
    except Exception as exc:
        report["errors"].append(f"Assets schemas: {_err(exc)}")
        schemas = []

    detailed = 0
    report["schemas"] = []
    for sc in schemas:
        if schema_filter and not _wanted(sc.get("name", "") + " " + sc.get("objectSchemaKey", ""), schema_filter):
            continue
        entry = {"id": sc.get("id"), "name": sc.get("name"), "key": sc.get("objectSchemaKey"),
                 "object_count": sc.get("objectCount"), "types": []}
        try:
            types = assets.object_types(sc["id"])
        except Exception as exc:
            report["errors"].append(f"object types of schema {sc.get('name')}: {_err(exc)}")
            types = []
        for ot in types:
            t = {"id": ot.get("id"), "name": ot.get("name"), "object_count": ot.get("objectCount"),
                 "parent_id": ot.get("parentObjectTypeId")}
            if _wanted(ot.get("name", ""), type_filter) and detailed < MAX_DETAILED_TYPES:
                detailed += 1
                try:
                    attrs = assets.attributes(ot["id"])
                    t["attributes"] = [{"name": a.get("name"), "type": describe_attribute(a),
                                        "description": a.get("description") or ""} for a in attrs]
                    names = {str(a.get("id")): a.get("name") for a in attrs}
                    if samples > 0:
                        objs = assets.aql(f'objectTypeId = {ot["id"]}', max_items=samples,
                                          page_size=max(samples, 1))
                        t["samples"] = [{"key": o.get("objectKey"), "label": o.get("label"),
                                         "attributes": _redact(object_attributes(o, names), redact)}
                                        for o in objs]
                except Exception as exc:
                    report["errors"].append(f"type {ot.get('name')}: {_err(exc)}")
            entry["types"].append(t)
        report["schemas"].append(entry)
    if detailed >= MAX_DETAILED_TYPES:
        report["errors"].append(f"only the first {MAX_DETAILED_TYPES} matching object types were detailed; "
                                "use --types to choose")
    return report


# ---------------------------------------------------------------------------
def format_report(r: dict) -> str:
    out: list[str] = []
    add = out.append
    add("=== Jira fields that may hold the affected service / routing ===")
    if r.get("jira_fields"):
        for f in r["jira_fields"]:
            add(f"  {f['id']:<22} {f['name']}   [{f['custom_type'] or f['type']}]")
    else:
        add("  (none matched)")

    if r.get("ticket"):
        add(f"\n=== Values on {r['ticket']['key']} ===")
        for k, v in r["ticket"]["fields"].items():
            add(f"  {k}: {json.dumps(v, ensure_ascii=False)[:600]}")

    add(f"\n=== Assets workspace: {r.get('workspace_id', 'not found')} ===")
    for sc in r.get("schemas", []):
        add(f"\n--- Schema: {sc['name']} (key {sc['key']}, id {sc['id']}, {sc.get('object_count')} objects) ---")
        for t in sc["types"]:
            mark = "*" if "attributes" in t else " "
            add(f" {mark} {t['name']:<40} id {t['id']:<6} {t.get('object_count')} objects")
        for t in sc["types"]:
            if "attributes" not in t:
                continue
            add(f"\n  >> {t['name']} (id {t['id']}) attributes:")
            for a in t["attributes"]:
                desc = f" — {a['description']}" if a["description"] else ""
                add(f"       {a['name']:<34} {a['type']}{desc}")
            for s in t.get("samples", []):
                add(f"     sample {s['key']} \"{s['label']}\":")
                for k, v in s["attributes"].items():
                    add(f"       {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
    add("\n(* = detailed below. Choose others with --types \"Name1,Name2\")")
    if r.get("errors"):
        add("\n=== Problems ===")
        for e in r["errors"]:
            add(f"  ! {e}")
    return "\n".join(out)
