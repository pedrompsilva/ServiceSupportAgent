"""Read-only client for Jira Service Management Assets (Cloud).

Assets lives on the Atlassian gateway, not the Jira site:
    https://api.atlassian.com/jsm/assets/workspace/<workspaceId>/v1/...
The workspace id is discovered from the Jira site (or set explicitly).
The same e-mail + API token basic auth as the Jira client is used.

Pagination is defensive on purpose: page sizes may be capped below what was
asked for, and some endpoints ignore startAt and return the first page again.
So we advance by the number of rows actually received, drop ids we have
already seen, and stop on isLast, an empty page or a page with nothing new.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable, Optional

GATEWAY = "https://api.atlassian.com/jsm/assets/workspace/{wid}/v1"


class AssetsError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


def _row_id(row: Any) -> str:
    if isinstance(row, dict) and row.get("id") is not None:
        return str(row["id"])
    return json.dumps(row, sort_keys=True, default=str)


def paginate(fetch_page: Callable[[int, int], Any], page_size: int = 50,
             max_items: Optional[int] = None, max_pages: int = 500) -> list:
    """fetch_page(start, size) returns a dict with 'values' (and maybe 'isLast') or a list."""
    seen: set[str] = set()
    out: list = []
    start = 0
    for _ in range(max_pages):
        page = fetch_page(start, page_size)
        values = page.get("values", []) if isinstance(page, dict) else (page or [])
        new = []
        for row in values:
            rid = _row_id(row)
            if rid not in seen:
                seen.add(rid)
                new.append(row)
        for row in new:
            out.append(row)
            if max_items and len(out) >= max_items:
                return out
        if not values or not new:
            break                                  # empty, or the same page again
        if isinstance(page, dict) and page.get("isLast") is True:
            break
        if not isinstance(page, dict):
            break                                  # plain lists are not paged
        start += len(values)                       # advance by what we actually got
    return out


class AssetsClient:
    def __init__(self, http, jira_base_url: str, workspace_id: Optional[str] = None):
        """http: object with request(method, url, **kw) -> response (httpx.Client or a fake)."""
        self.http = http
        self.jira_base_url = jira_base_url.rstrip("/")
        self._wid = workspace_id

    @classmethod
    def from_credentials(cls, jira_base_url: str, email: str, api_token: str,
                         workspace_id: Optional[str] = None, timeout: float = 60.0) -> "AssetsClient":
        import httpx  # lazy

        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        http = httpx.Client(headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
                            timeout=timeout)
        return cls(http, jira_base_url, workspace_id)

    # -- plumbing -------------------------------------------------------------
    def _req(self, method: str, url: str, **kw) -> Any:
        resp = self.http.request(method, url, **kw)
        if resp.status_code >= 400:
            raise AssetsError(resp.status_code, resp.text[:400])
        return resp.json() if resp.content else None

    def jira_get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._req("GET", f"{self.jira_base_url}{path}", params=params)

    @property
    def workspace_id(self) -> str:
        if not self._wid:
            data = self._req("GET", f"{self.jira_base_url}/rest/servicedeskapi/assets/workspace",
                             headers={"X-ExperimentalApi": "opt-in"})
            values = (data or {}).get("values", [])
            if not values:
                raise AssetsError(404, "no Assets workspace found on this Jira site")
            self._wid = values[0]["workspaceId"]
        return self._wid

    @property
    def base(self) -> str:
        return GATEWAY.format(wid=self.workspace_id)

    # -- reads ----------------------------------------------------------------
    def object(self, object_id: str) -> dict:
        return self._req("GET", f"{self.base}/object/{object_id}") or {}

    def schemas(self) -> list[dict]:
        return paginate(lambda s, n: self._req("GET", f"{self.base}/objectschema/list",
                                               params={"startAt": s, "maxResults": n}), page_size=50)

    def object_types(self, schema_id: str) -> list[dict]:
        data = self._req("GET", f"{self.base}/objectschema/{schema_id}/objecttypes/flat")
        return data if isinstance(data, list) else (data or {}).get("values", [])

    def attributes(self, object_type_id: str) -> list[dict]:
        data = self._req("GET", f"{self.base}/objecttype/{object_type_id}/attributes")
        return data if isinstance(data, list) else (data or {}).get("values", [])

    def aql(self, query: str, max_items: Optional[int] = None, page_size: int = 50) -> list[dict]:
        def fetch(start: int, size: int):
            return self._req("POST", f"{self.base}/object/aql",
                             params={"startAt": start, "maxResults": size, "includeAttributes": "true"},
                             json={"qlQuery": query})
        return paginate(fetch, page_size=page_size, max_items=max_items)


# ---------------------------------------------------------------------------
# Helpers to make Assets data readable
# ---------------------------------------------------------------------------
_DEFAULT_TYPES = {0: "Text", 1: "Integer", 2: "Boolean", 3: "Double", 4: "Date", 5: "Time",
                  6: "DateTime", 7: "URL", 8: "Email", 9: "Textarea", 10: "Select", 11: "IP Address"}
_KINDS = {0: "default", 1: "object", 2: "user", 3: "confluence", 4: "group", 5: "version",
          6: "project", 7: "status"}


def describe_attribute(a: dict) -> str:
    kind = a.get("type")
    if kind == 0:
        desc = (a.get("defaultType") or {}).get("name") or _DEFAULT_TYPES.get(
            (a.get("defaultType") or {}).get("id"), "Text")
    elif kind == 1:
        desc = f"Object → {(a.get('referenceObjectType') or {}).get('name', '?')}"
    else:
        desc = _KINDS.get(kind, f"type {kind}").capitalize()
    if (a.get("maximumCardinality") or 1) > 1 or a.get("maximumCardinality") == -1:
        desc += " (multiple)"
    return desc


def attribute_value(v: dict) -> Any:
    if v.get("referencedObject"):
        return v["referencedObject"].get("label")
    if v.get("user"):
        return v["user"].get("displayName")
    if v.get("group"):
        return v["group"].get("name")
    if v.get("status"):
        return v["status"].get("name")
    return v.get("displayValue") if v.get("displayValue") not in (None, "") else v.get("value")


def object_attributes(obj: dict, names_by_id: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in obj.get("attributes", []):
        name = names_by_id.get(str(attr.get("objectTypeAttributeId")), str(attr.get("objectTypeAttributeId")))
        values = [attribute_value(v) for v in attr.get("objectAttributeValues", [])]
        out[name] = values[0] if len(values) == 1 else values
    return out
