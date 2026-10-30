# MyWorldPanel Support Agent

Picks up new Jira tickets in the MyWorldPanel support project, triages them
with Claude, routes them to an internal team or an external vendor, and can
apply whitelisted fixes and resolve — **only as far as its autonomy level
allows**. It starts in shadow mode: it writes an internal recommendation
comment and changes nothing else.

## How a ticket flows

```
Jira Automation (issue created)
        │  POST /webhook/jira  {issueKey}  + X-Agent-Secret
        ▼
  re-read ticket from Jira ──► search similar tickets
        │
        ▼
  redact PII · wrap as untrusted data · Claude triage (forced tool schema)
        │
        ▼
  validate against config (components, fixes, duplicates)
        │
        ▼
  effective level = min(global cap, class level, severity cap,
                        confidence gate, injection gate)
        │
        ▼
  RICE score/band · deterministic routing from ownership matrix
        │
        ▼
  actions gated by level ─► L1 fields/labels  L2 assign / vendor e-mail / wait
                            L3 whitelisted fix  L4 resolve if fix verified
        │
        ▼
  internal audit comment (always) + issue property (idempotency)
```

The model never picks an assignee or e-mail address and never sees raw
personal data; code decides every action.

## Layout

| Path | Purpose |
|---|---|
| `config/policy.yaml` | Classifications, golden rules, severities, autonomy levels, guardrails, RICE |
| `config/assets.yaml` | Routing by Affected Service from Jira Assets: field ids, object types, pilot services, vendor groups |
| `config/ownership.yaml` | Component → team / vendor matrix (used only when Assets mode is off) |
| `config/fixes.yaml` | Whitelisted fix catalogue (enable one at a time) |
| `agent/pipeline.py` | Orchestration and autonomy gating |
| `agent/catalog.py`, `agent/assets_routing.py` | Assets catalogue (cached) and Service × Country → Assignment Group routing |
| `agent/triage.py` | Prompt, tool schema, Claude call, output validation |
| `agent/jira_client.py` | Jira Cloud REST v3 operations |
| `agent/vendor_email.py` | Vendor hand-off composition and Microsoft Graph sender |
| `agent/sanitize.py` | Redaction, prompt-injection heuristics |
| `agent/fixes/` | Fix implementations |
| `agent/webapp.py` | FastAPI webhook for Azure Container Apps |
| `backtest/run_backtest.py` | Replay a Jira CSV export and measure accuracy |
| `docs/SETUP.md` | Step-by-step setup, Azure deployment and rollout plan |

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in, then: set -a; . ./.env; set +a
python -m pytest -q
uvicorn agent.webapp:app --port 8080
```

Start with `AGENT_MAX_AUTONOMY=0`. See `docs/LOCAL_SETUP.md` to run it on a
laptop (CLI: `python -m agent.cli check | triage-file | run | poll`) and
`docs/SETUP.md` for the Azure deployment and rollout.
