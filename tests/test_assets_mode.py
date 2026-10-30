"""Routing by Affected Service from a (sample) Assets catalogue."""
import copy
import json
from types import SimpleNamespace

import pytest

from agent import assets_routing
from agent.catalog import Catalog, CatalogProvider, Category, Group, Mapping, Service, load_catalog, ticket_values
from agent.config import AgentConfig, load_config
from agent.models import Ticket
from agent.pipeline import Pipeline
from agent.triage import ClaudeTriage, system_prompt, tool_schema, user_prompt, validate_triage

from conftest import ROOT, FakeJira, FakeLLM, make_settings, triage_result

WID = "ws-1"
F_SERVICE, F_COUNTRY, F_GROUP, F_INC_CAT, F_ENV = (
    "customfield_26042", "customfield_26041", "customfield_26661", "customfield_26628", "customfield_26595")


def sample_catalog() -> Catalog:
    c = Catalog(workspace_id=WID)
    c.services = {"166": Service("166", "MyWorldpanel (MWP)", "Panelist app and portal"),
                  "200": Service("200", "ETL", "Data loads"),
                  "300": Service("300", "ISEC", "Info security")}
    c.countries = {"159": "Great Britain / United Kingdom", "160": "Ireland", "161": "Spain", "1": "Global"}
    c.environments = {"5004": "01 Production (Live)"}
    c.categories = {
        "5592": Category("5592", "System Errors/Instability - System Error Pop Ups", ["166"], ["Incident"]),
        "5600": Category("5600", "MWP - Access Request", ["166"], ["Service Request"]),
        "5700": Category("5700", "ETL - Load failure", ["200"], ["Incident"]),
    }
    c.groups = {"4795": Group("4795", "L1 - GB (MWP)", ["166"], ["159"]),
                "4796": Group("4796", "L1 - IE (MWP)", ["166"], ["160"]),
                "4900": Group("4900", "L3 / SME - ETL", ["200"], ["1"]),
                "4895": Group("4895", "3rd Party - Circana", ["300"], ["161"]),
                "4800": Group("4800", "Central Support Team", ["166"], ["161"]),
                "4801": Group("4801", "Spain MWP Squad", ["166"], ["161"])}
    c.mappings = [Mapping("m1", "MWP GB", ["166"], ["159"], "4795"),
                  Mapping("m2", "ETL", ["200"], ["1"], "4900"),
                  Mapping("m3", "ISEC ES", ["300"], ["161"], "4895")]
    return c


@pytest.fixture
def acfg(cfg):
    return copy.deepcopy(load_config(ROOT / "config").assets)


@pytest.fixture
def acfg_cfg(cfg, acfg):
    return AgentConfig(policy=copy.deepcopy(cfg.policy), ownership=cfg.ownership, fixes=cfg.fixes,
                       assets=acfg, catalog=sample_catalog())


def mwp_ticket(**assets):
    return Ticket(key="ITSM-3222", summary="client is getting system unavailable", issue_type="[System] Incident",
                  description="Panelists in GB see 'system unavailable' in the app since this morning.",
                  priority="Medium", assets=assets)


# ---------------------------------------------------------------- routing
def test_routing_mapping_match(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "166", "159", acting_allowed=True)
    assert (d.target, d.assignment_group, d.source) == ("internal", "L1 - GB (MWP)", "routing_mapping")


def test_global_country_in_mapping_matches_any_country(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "200", "161", acting_allowed=True)
    assert d.assignment_group == "L3 / SME - ETL"


def test_falls_back_to_assignment_group_lists(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "166", "160", acting_allowed=True)
    assert (d.assignment_group, d.source) == ("L1 - IE (MWP)", "assignment_groups")


def test_several_groups_go_to_human_with_candidates(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "166", "161", acting_allowed=True)
    assert d.target == "human_triage" and set(d.candidates) == {"Central Support Team", "Spain MWP Squad"}


def test_no_country_goes_to_human(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "166", None, acting_allowed=True)
    assert d.target == "human_triage" and "no Country" in d.reason and d.candidates


def test_vendor_group_and_guardrails(acfg):
    d = assets_routing.resolve(sample_catalog(), acfg, "300", "161", acting_allowed=True)
    assert d.target == "vendor" and d.assignment_group == "3rd Party - Circana"
    d = assets_routing.resolve(sample_catalog(), acfg, "166", "159", acting_allowed=False)
    assert d.target == "human_triage" and d.assignment_group == "L1 - GB (MWP)"


# ---------------------------------------------------------------- triage
def test_prompt_and_schema_in_assets_mode(acfg_cfg):
    sp = system_prompt(acfg_cfg)
    assert "SERVICES" in sp and "MyWorldpanel (MWP): Panelist app and portal" in sp
    assert "System Errors/Instability - System Error Pop Ups [Incident]" in sp
    assert "ETL - Load failure" not in sp                         # not a pilot service
    props = tool_schema(acfg_cfg)["input_schema"]["properties"]
    assert "service" in props and "component" not in props and "category" in props
    up = user_prompt(mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"], F_ENV: ["5004"]}), [], 12000, acfg_cfg)
    assert "Affected Service: MyWorldpanel (MWP)" in up and "Environment: 01 Production (Live)" in up


def test_ticket_outside_pilot_gets_its_own_categories(acfg_cfg):
    up = user_prompt(mwp_ticket(**{F_SERVICE: ["200"]}), [], 12000, acfg_cfg)
    assert "Categories of ETL: ETL - Load failure [Incident]" in up


def test_validation(acfg_cfg):
    t, notes = validate_triage(triage_result(classification="incident", component="myworldpanel (mwp)",
                                             country="ireland", category="ETL - Load failure"), acfg_cfg, [])
    assert t.component == "MyWorldpanel (MWP)" and t.country == "Ireland" and t.category is None
    assert any("does not belong" in n for n in notes)
    t, notes = validate_triage(triage_result(classification="incident", component="MyWorldpanel (MWP)",
                                             category="MWP - Access Request"), acfg_cfg, [])
    assert t.category is None and any("not a Incident category" in n for n in notes)
    t, _ = validate_triage(triage_result(component="Nope", country="Atlantis"), acfg_cfg, [])
    assert t.component is None and t.country is None


def test_claude_maps_service_to_component(acfg_cfg):
    data = triage_result().model_dump()
    data.pop("component")
    data["service"] = "MyWorldpanel (MWP)"
    resp = SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)])
    llm = ClaudeTriage("k", "m", client=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp)))
    assert llm.triage(mwp_ticket(), [], acfg_cfg).component == "MyWorldpanel (MWP)"


# ---------------------------------------------------------------- pipeline
def run(acfg_cfg, ticket, result, level, ignore_scope=False):
    policy = acfg_cfg.policy
    policy["autonomy_by_classification"] = {k: level for k in policy["autonomy_by_classification"]}
    jira = FakeJira(ticket)
    report = Pipeline(make_settings(level), acfg_cfg, jira, FakeLLM(result)).process(
        ticket.key, ignore_scope=ignore_scope)
    return report, jira


MWP_INCIDENT = dict(classification="incident", component="MyWorldpanel (MWP)",
                    category="System Errors/Instability - System Error Pop Ups", severity="S2")


def test_shadow_comment_shows_recommended_group(acfg_cfg):
    report, jira = run(acfg_cfg, mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"]}),
                       triage_result(**MWP_INCIDENT), level=0)
    text = jira.calls[0][3]
    assert jira.kinds() == ["comment", "property"]
    assert "Recommended assignment group: L1 - GB (MWP)" in text
    assert "Affected Service: MyWorldpanel (MWP)" in text and "raw_fields" not in text
    assert report.routing.source == "routing_mapping"


def test_level1_fills_only_empty_assets_fields(acfg_cfg):
    _, jira = run(acfg_cfg, mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"]}),
                  triage_result(**MWP_INCIDENT), level=1)
    upd = next(c for c in jira.calls if c[0] == "update_fields")
    raw = upd[5]
    assert set(raw) == {F_INC_CAT}                                 # service and country already set
    assert raw[F_INC_CAT] == [{"workspaceId": WID, "id": f"{WID}:5592", "objectId": "5592"}]
    assert not any(c[0] == "update_fields" and F_GROUP in (c[5] or {}) for c in jira.calls)


def test_level2_sets_group_and_corrects_service(acfg_cfg):
    ticket = mwp_ticket(**{F_SERVICE: ["200"], F_COUNTRY: ["159"], F_GROUP: ["4900"]})
    report, jira = run(acfg_cfg, ticket, triage_result(**{**MWP_INCIDENT, "severity": "S3"}), level=2,
                       ignore_scope=True)              # ticket says ETL, which is outside the pilot
    raws = [c[5] for c in jira.calls if c[0] == "update_fields" and c[5]]
    assert any(F_SERVICE in r and r[F_SERVICE][0]["objectId"] == "166" for r in raws)
    assert any(F_GROUP in r and r[F_GROUP][0]["objectId"] == "4795" for r in raws)
    text = next(c[3] for c in jira.calls if c[0] == "comment")
    assert "ticket says ETL — please check" in text and "(currently L3 / SME - ETL)" in text


def test_no_group_change_when_already_correct(acfg_cfg):
    ticket = mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"], F_GROUP: ["4795"]})
    report, _ = run(acfg_cfg, ticket, triage_result(**MWP_INCIDENT), level=2)
    assert not any(a.kind == "set_assignment_group" for a in report.actions)


def test_vendor_group_labelled_no_email(acfg_cfg):
    ticket = mwp_ticket(**{F_SERVICE: ["300"], F_COUNTRY: ["161"]})
    report, jira = run(acfg_cfg, ticket, triage_result(classification="incident", component="ISEC"), level=2,
                       ignore_scope=True)
    assert report.routing.target == "vendor"
    upd = next(c for c in jira.calls if c[0] == "update_fields" and c[4])
    assert "vendor-assigned" in upd[4]
    assert not any(a.kind == "email_vendor" for a in report.actions)


# ---------------------------------------------------------------- catalogue loading
class FakeAssets:
    workspace_id = WID

    def __init__(self, fail=False):
        self.fail = fail

    def attributes(self, tid):
        return [{"id": "1", "name": "Service"}, {"id": "2", "name": "Country"}, {"id": "3", "name": "Category Type"},
                {"id": "4", "name": "assignment_group"}, {"id": "5", "name": "Description of service"}]

    def aql(self, query, page_size=50, max_items=None):
        if self.fail:
            raise RuntimeError("Assets down")
        tid = query.split("=")[1].strip()
        ref = lambda i, l: {"referencedObject": {"id": i, "label": l}}
        return {
            "45": [{"id": "166", "label": "MyWorldpanel (MWP)", "attributes": [
                {"objectTypeAttributeId": "5", "objectAttributeValues": [{"value": "App"}]}]}],
            "44": [{"id": "159", "label": "Great Britain / United Kingdom"}],
            "228": [{"id": "5004", "label": "01 Production (Live)"}],
            "195": [{"id": "5592", "label": "Errors", "attributes": [
                {"objectTypeAttributeId": "1", "objectAttributeValues": [ref(166, "MWP")]},
                {"objectTypeAttributeId": "3", "objectAttributeValues": [{"value": "Incident"}]}]}],
            "194": [{"id": "4795", "label": "L1 - GB (MWP)", "attributes": [
                {"objectTypeAttributeId": "1", "objectAttributeValues": [ref(166, "MWP")]},
                {"objectTypeAttributeId": "2", "objectAttributeValues": [ref(159, "GB")]}]}],
            "294": [{"id": "9", "label": "MWP GB", "attributes": [
                {"objectTypeAttributeId": "1", "objectAttributeValues": [ref(166, "MWP")]},
                {"objectTypeAttributeId": "2", "objectAttributeValues": [ref(159, "GB")]},
                {"objectTypeAttributeId": "4", "objectAttributeValues": [ref(4795, "L1")]}]}],
        }[tid]


def test_load_catalog_and_snapshot_fallback(acfg, tmp_path):
    cat = load_catalog(FakeAssets(), acfg)
    assert cat.services["166"].description == "App"
    assert cat.categories["5592"].service_ids == ["166"] and cat.categories["5592"].types == ["Incident"]
    assert cat.mappings[0].group_id == "4795" and cat.environments["5004"] == "01 Production (Live)"
    snap = tmp_path / "snap.json"
    CatalogProvider(FakeAssets(), acfg, snap).get()
    assert json.loads(snap.read_text())["groups"]["4795"]["name"] == "L1 - GB (MWP)"
    again = CatalogProvider(FakeAssets(fail=True), acfg, snap).get()
    assert again.groups["4795"].country_ids == ["159"]


def test_ticket_values(acfg):
    v = ticket_values(mwp_ticket(**{F_SERVICE: ["166"], F_INC_CAT: ["5592"]}), acfg, sample_catalog())
    assert v["service"] == "MyWorldpanel (MWP)" and v["category"].startswith("System Errors") and v["country"] is None



# ---------------------------------------------------------------- intake / pilot scope
from agent.intake import in_scope, poll_jql  # noqa: E402


def test_scope_rules(acfg_cfg):
    assert in_scope(mwp_ticket(**{F_SERVICE: ["166"]}), acfg_cfg)[0]
    ok, why = in_scope(mwp_ticket(**{F_SERVICE: ["200"]}), acfg_cfg)
    assert not ok and "ETL" in why
    ok, why = in_scope(mwp_ticket(), acfg_cfg)
    assert not ok and "empty" in why
    ok, why = in_scope(mwp_ticket(**{F_SERVICE: ["166"]}).model_copy(update={"issue_type": "Bug"}), acfg_cfg)
    assert not ok and "issue type" in why


def test_out_of_scope_ticket_is_skipped_without_writes(acfg_cfg):
    report, jira = run(acfg_cfg, mwp_ticket(**{F_SERVICE: ["200"]}), triage_result(**MWP_INCIDENT), level=2)
    assert report.skipped.startswith("out of pilot scope") and jira.calls == []
    report, jira = run(acfg_cfg, mwp_ticket(), triage_result(**MWP_INCIDENT), level=0)
    assert "Affected Service is empty" in report.skipped and jira.calls == []


def test_poll_jql_filters_type_and_service(acfg_cfg):
    jql = poll_jql(acfg_cfg, 60)
    assert jql.startswith('project = "ITSM" AND issuetype in ("[System] Incident") AND cf[26042] in aqlFunction(')
    assert 'Name IN (\\"MyWorldpanel (MWP)\\")' in jql and "created >= -60m" in jql
    assert "aqlFunction" not in poll_jql(acfg_cfg, 60, with_service=False)
