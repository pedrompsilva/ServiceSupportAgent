"""Assets client pagination and the discovery report, against a fake HTTP layer."""
import json
from types import SimpleNamespace

from agent import cli
from agent.assets_client import AssetsClient, paginate
from agent.assets_discover import discover, format_report

from conftest import make_settings

JIRA = "https://example.atlassian.net"
WID = "ws-1"
BASE = f"https://api.atlassian.com/jsm/assets/workspace/{WID}/v1"


# ---------------------------------------------------------------- pagination
def test_paginate_advances_by_rows_received_when_page_capped():
    rows = [{"id": i} for i in range(60)]

    def fetch(start, size):                    # server caps pages at 25 whatever we ask
        chunk = rows[start:start + 25]
        return {"values": chunk, "isLast": start + 25 >= len(rows)}
    assert [r["id"] for r in paginate(fetch, page_size=50)] == list(range(60))


def test_paginate_stops_when_server_repeats_first_page():
    calls = []

    def fetch(start, size):
        calls.append(start)
        return {"values": [{"id": 1}, {"id": 2}], "isLast": False}
    assert len(paginate(fetch)) == 2 and len(calls) == 2


def test_paginate_drops_duplicates_and_respects_max_items():
    pages = [{"values": [{"id": 1}, {"id": 2}], "isLast": False},
             {"values": [{"id": 2}, {"id": 3}], "isLast": False},
             {"values": [{"id": 4}], "isLast": True}]
    it = iter(pages)
    assert [r["id"] for r in paginate(lambda s, n: next(it))] == [1, 2, 3, 4]
    assert len(paginate(lambda s, n: {"values": [{"id": i} for i in range(10)]}, max_items=3)) == 3


# ---------------------------------------------------------------- fake HTTP
class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, u), handler in self.routes.items():
            if m == method and u == url:
                data = handler(kw) if callable(handler) else handler
                return data if isinstance(data, Resp) else Resp(200, data)
        return Resp(404, {"errorMessages": ["not found"]})


ATTRS = [
    {"id": "10", "name": "Name", "type": 0, "defaultType": {"id": 0, "name": "Text"}},
    {"id": "11", "name": "Support Group", "type": 4},
    {"id": "12", "name": "Vendor", "type": 1, "referenceObjectType": {"name": "Supplier"}},
    {"id": "13", "name": "Vendor Email", "type": 0, "defaultType": {"id": 8, "name": "Email"}},
]
SERVICE_OBJ = {"id": "501", "objectKey": "ITSM-501", "label": "MyWorldPanel App", "attributes": [
    {"objectTypeAttributeId": "11", "objectAttributeValues": [{"group": {"name": "mwp-app-support"}}]},
    {"objectTypeAttributeId": "12", "objectAttributeValues": [{"referencedObject": {"label": "AppCo Ltd"}}]},
    {"objectTypeAttributeId": "13", "objectAttributeValues": [{"value": "help@appco.example", "displayValue": "help@appco.example"}]},
]}


def fake_assets(overrides=None):
    routes = {
        ("GET", f"{JIRA}/rest/api/3/field"): [
            {"id": "customfield_10500", "name": "Affected services", "custom": True,
             "schema": {"type": "array", "custom": "com.atlassian.jira.plugins.cmdb:cmdb-object-cftype"}},
            {"id": "customfield_10600", "name": "Story points", "custom": True, "schema": {"type": "number"}},
            {"id": "summary", "name": "Summary", "custom": False},
        ],
        ("GET", f"{JIRA}/rest/api/3/issue/ITSM-1"): {"fields": {
            "summary": "App crash for jane@corp.example",
            "customfield_10500": [{"workspaceId": WID, "id": f"{WID}:501", "objectId": "501"}]}},
        ("GET", f"{JIRA}/rest/servicedeskapi/assets/workspace"): {"values": [{"workspaceId": WID}]},
        ("GET", f"{BASE}/objectschema/list"): {"values": [{"id": "3", "name": "IT Services", "objectSchemaKey": "ITS",
                                                         "objectCount": 40}], "isLast": True},
        ("GET", f"{BASE}/objectschema/3/objecttypes/flat"): [
            {"id": "30", "name": "Business Service", "objectCount": 12},
            {"id": "31", "name": "Laptop", "objectCount": 900},
        ],
        ("GET", f"{BASE}/objecttype/30/attributes"): ATTRS,
        ("GET", f"{BASE}/objecttype/31/attributes"): ATTRS[:1],
        ("POST", f"{BASE}/object/aql"): {"values": [SERVICE_OBJ], "isLast": True},
    }
    routes.update(overrides or {})
    http = FakeHTTP(routes)
    return AssetsClient(http, JIRA), http


def test_discover_finds_field_types_attributes_and_samples():
    assets, http = fake_assets()
    r = discover(assets, ticket="ITSM-1")
    assert [f["id"] for f in r["jira_fields"]] == ["customfield_10500"]
    assert r["ticket"]["fields"]["customfield_10500"][0]["objectId"] == "501"
    assert "jane@corp.example" not in json.dumps(r)                 # redacted
    types = {t["name"]: t for t in r["schemas"][0]["types"]}
    assert "attributes" in types["Business Service"] and "attributes" not in types["Laptop"]
    svc = types["Business Service"]
    assert {"name": "Vendor", "type": "Object → Supplier", "description": ""} in svc["attributes"]
    sample = svc["samples"][0]["attributes"]
    assert sample["Support Group"] == "mwp-app-support" and sample["Vendor"] == "AppCo Ltd"
    assert sample["Vendor Email"] == "[EMAIL]"
    aql = next(c for c in http.calls if c[0] == "POST")
    assert aql[2]["json"] == {"qlQuery": "objectTypeId = 30"}
    text = format_report(r)
    assert "customfield_10500" in text and "* Business Service" in text and r["errors"] == []


def test_type_filter_and_no_redact():
    assets, _ = fake_assets()
    r = discover(assets, type_filter=["laptop"], redact=False)
    types = {t["name"]: t for t in r["schemas"][0]["types"]}
    assert "attributes" in types["Laptop"] and "attributes" not in types["Business Service"]


def test_permission_errors_are_reported_not_fatal():
    assets, _ = fake_assets({("GET", f"{BASE}/objectschema/list"): Resp(403, {"message": "forbidden"})})
    r = discover(assets)
    assert r["jira_fields"] and r["schemas"] == []
    assert any("object viewer" in e for e in r["errors"])


def test_cli_writes_report(tmp_path, capsys):
    assets, _ = fake_assets()
    out = tmp_path / "rep.json"
    rc = cli.cmd_assets_discover(make_settings(0), "itsm-1", None, None, 3, out, True, None, assets=assets)
    assert rc == 0 and json.loads(out.read_text())["workspace_id"] == WID
    assert "Full report written" in capsys.readouterr().out
