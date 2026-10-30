# Running the support agent on your laptop

Yes, the agent runs locally. The difference from Azure is how it picks up
tickets: Jira Cloud can't send webhooks to a laptop, so locally you either run
it on one ticket at a time or let it **poll** Jira every couple of minutes.

| Mode | Command | Needs Jira | Needs Claude | Writes to Jira |
|---|---|---|---|---|
| Health check | `python -m agent.cli check` | optional | optional | no |
| Triage a sample file | `python -m agent.cli triage-file samples\ticket.json` | no | yes | no |
| One ticket, dry run | `python -m agent.cli run MWPS-123` with `AGENT_DRY_RUN=true` | yes | yes | no |
| One ticket, shadow | same, `AGENT_DRY_RUN=false` | yes | yes | internal comment only |
| Poll for new tickets | `python -m agent.cli poll` | yes | yes | per autonomy level |
| Backtest | `python -m backtest.run_backtest export.csv` | CSV export only | yes | no |
| Assets discovery | `python -m agent.cli assets-discover --ticket ITSM-123` | yes | no | no |

Keep `AGENT_MAX_AUTONOMY=0` on a laptop. Use Azure for anything beyond shadow mode.

---

## Prerequisites

| Item | Detail |
|---|---|
| **Python 3.11 or newer** (3.12 recommended) | python.org installer or your Company Portal / software centre. No admin rights needed for the rest. |
| **The project folder** | `mwp-support-agent.zip`, unzipped |
| **Anthropic API key** | From console.anthropic.com, under your organisation's account. Set a monthly spend limit. Confirm data-handling approval before using real tickets. |
| **Jira Cloud API token** | Ideally the agent service account's. A personal token works for testing, but **anything the agent posts will appear as you**. |
| **Network access** | Outbound HTTPS to `api.anthropic.com` and `<your-site>.atlassian.net`. On a corporate network you may need the proxy settings and company CA certificate (see Troubleshooting). |
| **Permissions in Jira** | Browse the MWPS project, plus Add comments for shadow mode |
| Optional: Git, VS Code | For editing config and tracking changes |

---

## Step-by-step (Windows, PowerShell)

macOS/Linux equivalents are given where they differ.

### 1. Install Python

Install Python 3.12 and tick **"Add python.exe to PATH"**. Then check it:

```powershell
py --version          # macOS/Linux: python3 --version
```

### 2. Unzip the project

```powershell
mkdir C:\dev -ErrorAction SilentlyContinue
Expand-Archive "$HOME\Downloads\mwp-support-agent.zip" C:\dev
cd C:\dev\mwp-support-agent
```

### 3. Create a virtual environment and install dependencies

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1      # macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run this once and then retry:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 4. Run the tests

```powershell
python -m pytest -q
```

Expect `29 passed`. This proves the install works and needs no credentials.

### 5. Create your `.env` file

```powershell
copy .env.example .env            # macOS/Linux: cp .env.example .env
notepad .env
```

Fill in at least these values:

```
JIRA_BASE_URL=https://<your-site>.atlassian.net
JIRA_EMAIL=<account that owns the token>
JIRA_API_TOKEN=<token>
ANTHROPIC_API_KEY=<key>
CLAUDE_MODEL=claude-sonnet-5-5
AGENT_MAX_AUTONOMY=0
AGENT_DRY_RUN=true
```

- **Jira token:** id.atlassian.com → Security → *Create and manage API tokens*.
- **`COMMENT_VISIBILITY`:**
  - Keep `jsm_internal` if MWPS is a Jira Service Management project.
  - Otherwise use `role:<RoleName>`, e.g. `role:Developers`, so comments are not public.
- **`AGENT_ACCOUNT_ID`:** leave empty for now. Step 7 prints it.
- `.env` holds secrets. Never e-mail it or commit it to Git.

### 6. Set the project key in config

Open `config\policy.yaml` and set `project_key` to the real MyWorldPanel
support project key. Then put the real Jira component names in
`config\ownership.yaml`. Until you do, the agent will route most tickets to
human triage, which is safe but not useful.

### 7. Health check

```powershell
python -m agent.cli check
```

You should see `[ok]` for config, Jira and Claude. Copy the printed
`accountId` into `AGENT_ACCOUNT_ID` in `.env`. This stops the agent reacting
to its own comments.

### 8. Try Claude on the sample ticket (no Jira involved)

```powershell
python -m agent.cli triage-file samples\ticket.json
```

This prints the triage, RICE band and routing as JSON. Edit
`samples\ticket.json` to try your own wording. Real ticket text is fine, since it
is redacted before sending.

### 9. Dry run on a real ticket

With `AGENT_DRY_RUN=true` still set:

```powershell
python -m agent.cli run MWPS-1234
```

This reads the ticket and similar tickets from Jira, triages it, and **prints**
the comment it would post. Nothing is written to Jira. Try it on 10–20 recent
tickets and compare with what your team actually did.

### 10. Shadow mode on a test ticket

1. Create a test ticket in MWPS.
2. Set `AGENT_DRY_RUN=false`, then run:
   ```powershell
   python -m agent.cli run MWPS-<test ticket> --force
   ```
3. Open the ticket in Jira and **confirm the comment is internal**: it should not be
   visible to the reporter or customer portal. Only then use it on real tickets.

### 11. Poll for new tickets

```powershell
python -m agent.cli poll --interval 120 --lookback 60
```

- Every 2 minutes, the agent processes tickets created in the last 60 minutes that aren't done.
- Already-processed tickets are skipped. Stopping and restarting is safe.
- The laptop must stay awake and online. Stop with **Ctrl+C**.

### 12. Backtest against history

In Jira, open a filter of ~300 resolved MWPS tickets → *Export* → *CSV (all fields)*. Then run:

```powershell
python -m backtest.run_backtest "$HOME\Downloads\Jira.csv" --limit 300 --out results --calibrate-rice
```

Read `results\summary.md`. This is the evidence for whether to widen autonomy later.

### 13. Discover how routing is modelled in Jira Assets (read-only)

ITSM routes by **Affected service**, an Assets object field, with a ticket
routing mapping held in Assets. To show the agent how that is modelled, run:

```powershell
python -m agent.cli assets-discover --ticket ITSM-3222
```

It prints:
- the Jira fields that look like Affected service or routing fields, with their ids
- the raw values of those fields on the ticket you name
- every Assets schema and object type, with object counts
- for object types whose names suggest services or routing (service, routing,
  assignment, vendor, supplier, team, group, owner, support): their attributes
  and 3 sample objects

To detail specific types, use `--types "Business Service,Ticket Routing"`. To
skip sample objects, add `--samples 0`.

Sample values are **redacted** by default (e-mails, phone numbers, long numbers),
so the printed output is safe to paste back for the next build step. The full
report is also saved to `assets_discovery.json`, which Git ignores. `--no-redact`
is for your own local inspection only.

Needs read access to the Assets schema (the *object viewer* role or higher). A
403 shows up under "Problems" with that hint, and the rest of the report still
prints.

---

## Cost and safety notes

- Each ticket is one Claude call. At pilot volumes that is small, but set a spend limit in the Anthropic console anyway.
- Stick to `AGENT_MAX_AUTONOMY=0` locally. Levels 2+ (assigning, vendor e-mails, fixes) belong on the Azure deployment with the service account, so actions are attributable and run continuously.
- With a personal token, the comments are yours. Tell the team before running shadow mode on live tickets.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | Corporate SSL inspection. Ask IT for the company root CA bundle (`.pem`), then set `SSL_CERT_FILE=C:\path\company-ca.pem` in `.env`. |
| Connection timeouts | Corporate proxy. Add `HTTPS_PROXY=http://proxy.company:port` to `.env`. |
| Jira `401` | Email doesn't match the token owner, or the token has expired. |
| Jira `404` on the ticket | Wrong key, or the account can't browse the project |
| Jira `400` when commenting | `jsm_internal` used on a non-JSM project. Set `COMMENT_VISIBILITY=role:<Role>`. |
| Claude `not_found_error` for the model | Check `CLAUDE_MODEL` against the models available to your API key |
| "already processed, no new information" | Expected. Use `--force` to re-run. |
| `Activate.ps1 cannot be loaded` | See step 3 (execution policy), or use `cmd` and `.venv\Scripts\activate.bat`. |
| Everything routes to `human_triage` | Component names in `ownership.yaml` don't match Jira, or confidence is below `min_confidence_to_act` |

### Optional: webhook mode on a laptop

You can run the web app (`uvicorn agent.webapp:app --port 8080`) and expose it
through a tunnel so Jira Automation can reach it. Only do this with IT approval,
because it opens a public URL to your machine. Polling gives the same result
for a pilot.
