import csv

from backtest.run_backtest import main, read_jira_csv

from conftest import FakeLLM, triage_result


def write_export(path):
    header = ["Summary", "Issue key", "Issue Type", "Status", "Priority", "Component/s", "Component/s",
              "Labels", "Description"]
    rows = [
        ["Points missing", "MWPS-1", "Bug", "Done", "Medium", "Rewards & Incentives", "", "x", "no points"],
        ["Portal 500", "MWPS-2", "Incident", "Done", "High", "Panelist Portal", "", "", "500 error"],
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def test_reads_duplicate_columns(tmp_path):
    p = tmp_path / "export.csv"
    write_export(p)
    tickets = read_jira_csv(p)
    assert [t.key for t in tickets] == ["MWPS-1", "MWPS-2"]
    assert tickets[0].components == ["Rewards & Incentives"]


def test_backtest_end_to_end(tmp_path, capsys):
    p = tmp_path / "export.csv"
    write_export(p)
    llm = FakeLLM(triage_result())  # always predicts Bug / Rewards
    rc = main([str(p), "--out", str(tmp_path / "out"), "--calibrate-rice"], llm=llm)
    assert rc == 0
    # the model must not see the real answers
    assert all(t.components == [] and t.issue_type == "" for t in llm.seen)
    summary = (tmp_path / "out" / "summary.md").read_text()
    assert "Component match | 50% (1/2)" in summary
    assert (tmp_path / "out" / "results.csv").exists()
    assert "Suggested rice.bands" in capsys.readouterr().out


def test_backtest_jql_assets_mode(tmp_path, capsys):
    from test_assets_mode import F_COUNTRY, F_GROUP, F_INC_CAT, F_SERVICE, MWP_INCIDENT, mwp_ticket, sample_catalog

    tickets = {"ITSM-1": mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"], F_INC_CAT: ["5592"], F_GROUP: ["4795"]}),
               "ITSM-2": mwp_ticket(**{F_SERVICE: ["166"], F_COUNTRY: ["159"], F_GROUP: ["4800"]})}
    jira = type("J", (), {"search_keys": lambda self, jql, limit=200: list(tickets),
                          "get_ticket": lambda self, k: tickets[k].model_copy(update={"key": k})})()
    provider = type("P", (), {"get": lambda self, force=False: sample_catalog()})()
    llm = FakeLLM(triage_result(**MWP_INCIDENT))
    rc = main(["--jql", "project = ITSM", "--out", str(tmp_path / "o")], llm=llm, jira=jira, catalog_provider=provider)
    assert rc == 0
    seen = llm.seen[0].assets
    assert F_SERVICE not in seen and F_GROUP not in seen and F_INC_CAT not in seen and F_COUNTRY in seen
    summary = (tmp_path / "o" / "summary.md").read_text()
    assert "Affected Service | 100% (2/2)" in summary and "Assignment group | 50% (1/2)" in summary
    assert "Central Support Team → L1 - GB (MWP): 1" in summary
