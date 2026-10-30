import json
import os
from pathlib import Path

from agent import cli
from agent.config import load_config
from agent.pipeline import Pipeline

from conftest import ROOT, FakeJira, FakeLLM, make_settings, triage_result


def test_dotenv_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nFOO_A=one\nFOO_B="two" # trailing\nFOO_C=\n', encoding="utf-8")
    monkeypatch.setenv("FOO_A", "kept")
    for k in ("FOO_B", "FOO_C"):
        monkeypatch.delenv(k, raising=False)
    cli.load_dotenv(env)
    assert os.environ["FOO_A"] == "kept" and os.environ["FOO_B"] == "two"


def test_poll_jql():
    jql = cli.poll_jql("MWPS", 60)
    assert 'project = "MWPS"' in jql and "created >= -60m" in jql


def test_triage_file_offline(capsys):
    s = make_settings(0)
    rc = cli.cmd_triage_file(s, ROOT / "samples" / "ticket.json", llm=FakeLLM(triage_result()))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["routing"]["target"] == "vendor" and out["rice"]["band"]


def test_poll_once_processes_new_tickets(cfg, ticket, capsys):
    jira = FakeJira(ticket)
    jira.search_keys = lambda jql, limit=50: [ticket.key]
    p = Pipeline(make_settings(0), cfg, jira, FakeLLM(triage_result()))
    cli.cmd_poll(make_settings(0), interval=1, lookback=60, once=True, pipeline=p)
    assert "[Support Agent] Triage" in capsys.readouterr().out
    assert "comment" in jira.kinds()


class _Provider:
    def get(self, force=False):
        from test_assets_mode import sample_catalog
        return sample_catalog()


def test_assets_catalog_summary_and_route(capsys):
    rc = cli.cmd_assets_catalog(make_settings(0), ["MyWorldpanel (MWP)", "Great Britain / United Kingdom"],
                                provider=_Provider())
    out = capsys.readouterr().out
    assert rc == 0 and "3 services" in out and "MyWorldpanel (MWP): 2 categories" in out
    assert "rule 'MWP GB': 1 countries → L1 - GB (MWP)" in out
    assert "Route MyWorldpanel (MWP) / Great Britain / United Kingdom: internal" in out
    assert "vendor_groups not found in Assets: ['3rd Party - Kyndryl']" in out


def test_triage_file_in_assets_mode(capsys):
    from test_assets_mode import MWP_INCIDENT
    rc = cli.cmd_triage_file(make_settings(0), ROOT / "samples" / "ticket.json",
                             llm=FakeLLM(triage_result(**{**MWP_INCIDENT, "country": "Great Britain / United Kingdom"})),
                             provider=_Provider())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["routing"]["assignment_group"] == "L1 - GB (MWP)"
