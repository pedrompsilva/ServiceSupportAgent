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
