"""The routing catalogue loaded from Jira Assets: services, countries,
categories, assignment groups and the routing_mapping rules.

Loaded with AQL per object type, kept in memory for `cache_minutes`, and
saved to a local snapshot so the agent can keep working (and the backtest can
be repeated) when Assets can't be reached.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class Service:
    id: str
    name: str
    description: str = ""


@dataclass
class Category:
    id: str
    name: str
    service_ids: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)


@dataclass
class Group:
    id: str
    name: str
    service_ids: list[str] = field(default_factory=list)
    country_ids: list[str] = field(default_factory=list)


@dataclass
class Mapping:
    id: str
    name: str
    service_ids: list[str] = field(default_factory=list)
    country_ids: list[str] = field(default_factory=list)
    group_id: Optional[str] = None


@dataclass
class Catalog:
    workspace_id: str
    services: dict[str, Service] = field(default_factory=dict)
    countries: dict[str, str] = field(default_factory=dict)          # id -> name
    environments: dict[str, str] = field(default_factory=dict)       # id -> name
    categories: dict[str, Category] = field(default_factory=dict)
    groups: dict[str, Group] = field(default_factory=dict)
    mappings: list[Mapping] = field(default_factory=list)
    loaded_at: float = 0.0

    # -- lookups ----------------------------------------------------------------
    @staticmethod
    def _by_name(items: dict, name: Optional[str], attr: Optional[str] = "name") -> Optional[str]:
        if not name:
            return None
        n = name.strip().lower()
        for k, v in items.items():
            label = getattr(v, attr) if attr else v
            if (label or "").strip().lower() == n:
                return k
        return None

    def service_id(self, name: Optional[str]) -> Optional[str]:
        return self._by_name(self.services, name)

    def country_id(self, name: Optional[str]) -> Optional[str]:
        return self._by_name(self.countries, name, attr=None)

    def category_id(self, name: Optional[str]) -> Optional[str]:
        return self._by_name(self.categories, name)

    def name_of(self, kind: str, obj_id: Optional[str]) -> Optional[str]:
        if not obj_id:
            return None
        if kind == "country":
            return self.countries.get(obj_id)
        if kind == "environment":
            return self.environments.get(obj_id)
        coll = {"service": self.services, "category": self.categories, "group": self.groups}[kind]
        item = coll.get(obj_id)
        return item.name if item else None

    def offered_services(self, pilot: list[str]) -> list[Service]:
        if not pilot:
            return sorted(self.services.values(), key=lambda s: s.name.lower())
        wanted = {p.strip().lower() for p in pilot}
        return [s for s in self.services.values() if s.name.strip().lower() in wanted]

    def categories_for(self, service_ids: list[str]) -> list[Category]:
        ids = set(service_ids)
        return sorted([c for c in self.categories.values() if ids & set(c.service_ids)],
                      key=lambda c: c.name.lower())

    def ref(self, obj_id: str) -> dict:
        """The value shape Jira uses for an Assets object field."""
        return {"workspaceId": self.workspace_id, "id": f"{self.workspace_id}:{obj_id}", "objectId": obj_id}

    # -- snapshot ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Catalog":
        return cls(
            workspace_id=d["workspace_id"],
            services={k: Service(**v) for k, v in d.get("services", {}).items()},
            countries=dict(d.get("countries", {})),
            environments=dict(d.get("environments", {})),
            categories={k: Category(**v) for k, v in d.get("categories", {}).items()},
            groups={k: Group(**v) for k, v in d.get("groups", {}).items()},
            mappings=[Mapping(**m) for m in d.get("mappings", [])],
            loaded_at=d.get("loaded_at", 0.0),
        )


# ---------------------------------------------------------------------------
# Loading from Assets
# ---------------------------------------------------------------------------
def _attr_index(obj: dict, names_by_id: dict[str, str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in obj.get("attributes", []):
        name = names_by_id.get(str(a.get("objectTypeAttributeId")))
        if name:
            out[name] = a.get("objectAttributeValues", [])
    return out


def _ref_ids(values: list[dict]) -> list[str]:
    return [str(v["referencedObject"]["id"]) for v in values
            if isinstance(v.get("referencedObject"), dict) and v["referencedObject"].get("id") is not None]


def _texts(values: list[dict]) -> list[str]:
    out = []
    for v in values:
        val = v.get("displayValue") if v.get("displayValue") not in (None, "") else v.get("value")
        if val not in (None, ""):
            out.append(str(val))
    return out


def load_catalog(assets, acfg: dict) -> Catalog:
    """assets: an AssetsClient. acfg: the parsed config/assets.yaml."""
    types, attrs = acfg["object_types"], acfg["attributes"]

    def load(kind: str) -> tuple[list[dict], dict[str, str]]:
        tid = types[kind]
        names = {str(a.get("id")): a.get("name") for a in assets.attributes(str(tid))}
        return assets.aql(f"objectTypeId = {tid}", page_size=50), names

    cat = Catalog(workspace_id=assets.workspace_id, loaded_at=time.time())

    objs, names = load("service")
    for o in objs:
        ix = _attr_index(o, names)
        desc = " ".join(_texts(ix.get(attrs.get("service_description", ""), [])))
        cat.services[str(o["id"])] = Service(id=str(o["id"]), name=o.get("label", ""), description=desc)

    objs, _ = load("country")
    for o in objs:
        cat.countries[str(o["id"])] = o.get("label", "")

    if types.get("environment"):
        objs, _ = load("environment")
        for o in objs:
            cat.environments[str(o["id"])] = o.get("label", "")

    objs, names = load("category")
    for o in objs:
        ix = _attr_index(o, names)
        cat.categories[str(o["id"])] = Category(
            id=str(o["id"]), name=o.get("label", ""),
            service_ids=_ref_ids(ix.get(attrs["category_service"], [])),
            types=_texts(ix.get(attrs["category_type"], [])))

    objs, names = load("assignment_group")
    for o in objs:
        ix = _attr_index(o, names)
        cat.groups[str(o["id"])] = Group(
            id=str(o["id"]), name=o.get("label", ""),
            service_ids=_ref_ids(ix.get(attrs["group_services"], [])),
            country_ids=_ref_ids(ix.get(attrs["group_countries"], [])))

    objs, names = load("routing_mapping")
    for o in objs:
        ix = _attr_index(o, names)
        groups = _ref_ids(ix.get(attrs["mapping_group"], []))
        cat.mappings.append(Mapping(
            id=str(o["id"]), name=o.get("label", ""),
            service_ids=_ref_ids(ix.get(attrs["mapping_services"], [])),
            country_ids=_ref_ids(ix.get(attrs["mapping_countries"], [])),
            group_id=groups[0] if groups else None))
    return cat


class CatalogProvider:
    """Caches the catalogue; falls back to the last snapshot if Assets fails."""

    def __init__(self, assets, acfg: dict, snapshot: Optional[Path] = None):
        self.assets, self.acfg = assets, acfg
        self.ttl = float(acfg.get("cache_minutes", 60)) * 60
        self.snapshot = snapshot
        self._cat: Optional[Catalog] = None

    def get(self, force: bool = False) -> Catalog:
        fresh = self._cat and (time.time() - self._cat.loaded_at) < self.ttl
        if self._cat and fresh and not force:
            return self._cat
        try:
            self._cat = load_catalog(self.assets, self.acfg)
            if self.snapshot:
                self.snapshot.write_text(json.dumps(self._cat.to_dict(), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            if self._cat:
                log.warning("Assets reload failed, keeping cached catalogue: %s", exc)
            elif self.snapshot and self.snapshot.exists():
                log.warning("Assets unavailable, using snapshot %s: %s", self.snapshot, exc)
                self._cat = Catalog.from_dict(json.loads(self.snapshot.read_text(encoding="utf-8")))
            else:
                raise
        return self._cat


# ---------------------------------------------------------------------------
# Reading a ticket's current Assets values
# ---------------------------------------------------------------------------
def category_field(acfg: dict, classification: Optional[str]) -> Optional[str]:
    fields = acfg["fields"].get("category") or {}
    if isinstance(fields, str):
        return fields
    return fields.get(classification or "") or next(iter(fields.values()), None)


def ticket_values(ticket, acfg: dict, cat: Catalog) -> dict[str, Any]:
    """Current service / country / category / group on the ticket (ids and names)."""
    f = acfg["fields"]

    def first(field_id: Optional[str]) -> Optional[str]:
        ids = ticket.assets.get(field_id or "", []) if field_id else []
        return ids[0] if ids else None

    cat_fields = f.get("category") or {}
    cat_fields = [cat_fields] if isinstance(cat_fields, str) else list(dict.fromkeys(cat_fields.values()))
    category = next((first(fid) for fid in cat_fields if first(fid)), None)
    out = {
        "service_id": first(f.get("service")),
        "country_id": first(f.get("country")),
        "category_id": category,
        "group_id": first(f.get("assignment_group")),
        "environment_id": first(f.get("environment")),
    }
    out["service"] = cat.name_of("service", out["service_id"])
    out["country"] = cat.name_of("country", out["country_id"])
    out["category"] = cat.name_of("category", out["category_id"])
    out["group"] = cat.name_of("group", out["group_id"])
    out["environment"] = cat.name_of("environment", out["environment_id"])
    return out
