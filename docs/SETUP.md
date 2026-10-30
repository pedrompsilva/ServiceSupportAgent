# Setup & rollout guide — MyWorldPanel Support Agent

Work through the steps in order. Steps 1–3 need decisions and approvals;
steps 4–7 are technical; steps 8–10 are the pilot itself.

---

## 1. Fill in the configuration (owner: Service Support lead + BA)

| File | What to replace | Source |
|---|---|---|
| `config/policy.yaml` | `project_key`, classification descriptions, golden rules, Jira priority names, issue type names, transition names, human-triage account | MWP A4 Incident/Defect workflow; Jira project settings |
| `config/ownership.yaml` | Real component names, team owners, default assignees, vendor contacts and SLA days | Jira components list; vendor contracts; defect governance model |
| `config/fixes.yaml` | Leave only `request_more_info` enabled for now | — |

Every value marked `TO CONFIRM` is a placeholder. Component names must match
Jira exactly — the agent is only allowed to pick from this list.

To get account ids: `GET https://<site>.atlassian.net/rest/api/3/user/search?query=<name>`
(or open the person's profile — the id is in the URL).

## 2. Approvals (owner: you)

- **Data handling:** ticket summaries, descriptions and comments are sent to the
  Anthropic API after redaction of e-mails, phone numbers, long digit strings and
  secrets. Get InfoSec / DPO sign-off and an organisation API key.
- **Vendor e-mail:** confirm that sending redacted ticket details to each vendor
  is covered by the existing contract / DPA.

## 3. Jira service account (owner: Jira admin)

1. Create a dedicated Atlassian account, e.g. `mwp-support-agent@<company>`.
   If MWPS is a Jira Service Management project it needs an **agent licence**
   to post internal comments.
2. Add it to the MWPS project with a role that can: Browse, Add comments,
   Edit issues, Assign issues, Transition issues. Nothing outside MWPS.
3. Create an API token for it (id.atlassian.com → Security → API tokens).
4. Note its accountId (`GET /rest/api/3/myself` with its token) → `AGENT_ACCOUNT_ID`.

## 4. Azure resources (owner: platform team)

```bash
RG=rg-mwp-support-agent
LOC=westeurope
ACR=acrmwpsupportagent          # must be globally unique
APP=mwp-support-agent
ENV=cae-mwp-support

az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic
az acr build -r $ACR -t $APP:0.1.0 .          # run from the repo root

az containerapp env create -g $RG -n $ENV -l $LOC

az containerapp create -g $RG -n $APP --environment $ENV \
  --image $ACR.azurecr.io/$APP:0.1.0 --registry-server $ACR.azurecr.io \
  --ingress external --target-port 8080 --min-replicas 1 --max-replicas 1 \
  --secrets jira-token=<token> anthropic-key=<key> webhook-secret=<random> graph-secret=<secret> \
  --env-vars \
    JIRA_BASE_URL=https://<site>.atlassian.net \
    JIRA_EMAIL=mwp-support-agent@<company> \
    JIRA_API_TOKEN=secretref:jira-token \
    AGENT_ACCOUNT_ID=<accountId> \
    ANTHROPIC_API_KEY=secretref:anthropic-key \
    CLAUDE_MODEL=claude-sonnet-5-5 \
    WEBHOOK_SECRET=secretref:webhook-secret \
    AGENT_MAX_AUTONOMY=0
```

Recommended before go-live:

- Move the secrets into **Key Vault** and reference them from the Container App
  with a managed identity.
- Restrict ingress to **Atlassian's published egress IP ranges** (Container Apps
  IP restrictions), or put Azure API Management in front.
- Logs go to Log Analytics; every processed ticket emits one JSON line with
  `"event": "ticket_processed"` — build the alert and audit queries on that.
- Keep `--max-replicas 1` during the pilot. For production, put an Azure
  Service Bus queue between the webhook and the pipeline (retries, back-pressure,
  dead-lettering) — `agent/webhook.py` is framework-free so the consumer can
  reuse it.

## 5. Vendor e-mail via Microsoft Graph (owner: M365 admin) — needed from level 2

1. Create a shared mailbox, e.g. `mwp-support@<company>` → `GRAPH_SENDER`.
2. Entra ID → App registration → add **Mail.Send (Application)** → grant admin consent.
3. **Restrict the app to that one mailbox** (Exchange Online RBAC for
   Applications, or an Application Access Policy). Without this the app could
   send as any user in the tenant.
4. Add `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` (as a secret).

Until Graph is configured, vendor hand-offs appear as drafts in the internal
comment and are never sent.

## 6. Jira Automation rule (owner: Jira admin)

Project MWPS → Automation → Create rule:

- **Trigger:** Issue created. (Add a second rule later for *Issue commented*,
  so answers to the agent's questions are re-triaged.)
- **Condition:** *User condition* → initiator is **not** the agent service account.
- **Action:** Send web request
  - URL: `https://<container-app-fqdn>/webhook/jira`
  - Method: POST, Body: Custom data
  - Headers: `Content-Type: application/json`, `X-Agent-Secret: <WEBHOOK_SECRET>`
  - Custom data:
    ```json
    {"issueKey": "{{issue.key}}", "event": "created", "actor": "{{initiator.accountId}}"}
    ```

The agent only trusts the issue key from the payload and re-reads the ticket
from Jira itself.

## 7. Smoke test

```bash
curl https://<fqdn>/healthz
curl -X POST -H "X-Agent-Secret: <secret>" https://<fqdn>/run/MWPS-<test-ticket>
```

Expect one internal comment starting with `[Support Agent] Triage — mode: shadow`.

## 8. Backtest before the live pilot

1. In Jira, filter ~300 resolved MWPS tickets from the last 6 months →
   Export → CSV (all fields).
2. Run locally (nothing is written to Jira):
   ```bash
   pip install -r requirements.txt
   export ANTHROPIC_API_KEY=...
   python -m backtest.run_backtest export.csv --limit 300 --out results/ --calibrate-rice
   ```
3. Review `results/summary.md`. Look at confident component misses first —
   they usually mean the component descriptions or ownership matrix need work,
   not the model.
4. Paste the suggested RICE bands into `policy.yaml` if they look right.

## 9. Shadow pilot (4 weeks suggested)

- Agent runs at level 0 on every new MWPS ticket.
- Triagers add `ai-triage-correct` or `ai-triage-wrong` to each ticket.
- Weekly review JQL:
  ```
  project = MWPS AND labels in (ai-triage-correct, ai-triage-wrong) AND created >= -7d
  ```
- Tune `policy.yaml` / `ownership.yaml` from the wrong ones; redeploy.

## 10. Promotion checklist (per classification)

Raise `autonomy_by_classification.<class>` one level at a time when:

- [ ] ≥ 50 shadow tickets of that class reviewed
- [ ] confident-slice accuracy ≥ 90% for classification **and** component
- [ ] no wrong vendor routing in the last 2 weeks
- [ ] owning team lead agrees
- [ ] then raise `AGENT_MAX_AUTONOMY` to at least that level

S1 stays at level 0 (human first) by default — see `guardrails.severity_cap`.

## Not built yet (phase 2)

- **Vendor replies back into Jira:** poll or subscribe to the shared mailbox via
  Graph and add replies as comments on the ticket whose key is in the subject.
- **Knowledge-base lookup:** Confluence search for runbooks as extra context.
- **Real fixes:** implement `rerun_failed_ingestion_job` etc. in
  `agent/fixes/catalogue.py` once the ops endpoints exist.
- **Service Bus queue** and retry policy for production volumes.
