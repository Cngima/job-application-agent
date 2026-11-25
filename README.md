# job-application-agent

A personal job-search agent for remote AI automation and AI project management roles. It
finds, routes, scores, drafts and tracks applications. **It never submits an application,
never emails an employer and never signs in to a job site.** The owner reviews and submits
everything.

> **Status:** under active development, built one stage at a time (see [PLAN.md](PLAN.md)).
> This README only describes what is built and tested today. Fit scoring, drafting, the
> digest, dashboard, MCP server and n8n workflow are planned, not built.

## What works today

| Stage | What it does | Status |
|---|---|---|
| 1. Evidence bank | Parses CV `.docx` files into one fact per bullet, with source files, tools and metrics. Owner review decisions live in an overrides file, so a rebuild never loses them. | Built, tested |
| 2. Sourcing | Reads public ATS job boards (Greenhouse, Lever, Ashby, Workable, Breezy, Zoho Recruit) and discovery feeds (RemoteOK, Himalayas, Jobicy, MyJobMag Kenya). Drops postings older than 7 days. Traces aggregator leads back to the employer's own posting. | Built, tested, run on real data |
| 3. Routing | Sends every job to APPLY, REVIEW, SKIP, CHECK_ELIGIBILITY or LINKEDIN_LEAD. Free rules first (location field, pay floor, title, scam flags), then an AI call for the rest, then policy checks the AI cannot override. Every decision stores its quote and reasons. | Built, tested, run on real data |
| Intake | WhatsApp inbox folder, `jobagent add` for single jobs or batches, LinkedIn job alert emails (read-only Gmail), scam checks. | Built, tested |

## Rules the code enforces

- **Eligibility comes from the employer's own posting.** Aggregator location labels are stored
  separately and never used to decide eligibility. Every job records where its text came from:
  `employer`, `aggregator`, `community` or `pasted`.
- **Polite reading:** official APIs, public ATS endpoints and public RSS only; `robots.txt`
  checked on every host (including every redirect hop); at most one request every 2 seconds per
  host; a User-Agent that names this project. Never bypasses bot protection or logins.
- **Never opened:** LinkedIn, Indeed, WhatsApp, Telegram and social media pages.
- **No WhatsApp access of any kind.** No WhatsApp Web automation and no unofficial WhatsApp
  libraries. You copy posts into a text file yourself.
- **Gmail is read-only** (`gmail.readonly` scope only). The agent cannot send, delete, label or
  mark mail as read; it remembers processed emails in its own database.
- **Scam checks** on WhatsApp, pasted and Reddit posts. A flagged job carries plain-English
  reasons and will go to REVIEW, never APPLY.
- **Private data stays private:** `.env`, `data/private/` and `inbox/` are gitignored. The
  public repo ships a fictional sample profile.

## Setup (Windows)

```powershell
git clone https://github.com/Cngima/job-application-agent
cd job-application-agent
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
.venv\Scripts\jobagent --help
```

Use `--profile sample` for the fictional demo data and `--profile private` for your own.

### Daily commands available now

```powershell
jobagent --profile private source          # read all job boards and feeds
jobagent --profile private route           # route new jobs (rules, then AI)
jobagent --profile private decisions       # best jobs first, with quotes and reasons
jobagent --profile private intake          # WhatsApp inbox, then LinkedIn alerts (if set up)
jobagent --profile private prune           # remove stored jobs older than 7 days (backs up first)
```

### WhatsApp job channels: the inbox folder

1. Create the inbox folder and a **Job Inbox** shortcut on your desktop (one time):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\create_inbox_shortcut.ps1
   ```

2. In WhatsApp, open a channel, select or copy job posts (or use *Export chat*, *Without media*).
3. Paste into Notepad and save as a `.txt` file (UTF-8) in the **Job Inbox** folder.
   Separate copied posts with a line of dashes (`-----`) or two blank lines; exported chats
   with timestamps are split automatically.
4. `jobagent --profile private inbox` (or the daily `intake`) splits each file into posts,
   removes duplicates, follows apply links where `robots.txt` allows, runs the scam checks and
   stores the jobs with source `whatsapp`. Processed files move to `inbox\processed\`.

Try it on the sample file: `jobagent --profile sample add --batch data\sample\whatsapp_example.txt`.

### Single jobs from anywhere (X, LinkedIn, hiring.cafe, a friend)

```powershell
jobagent --profile private add https://boards.greenhouse.io/<company>/jobs/<id>
jobagent --profile private add "Position: ... Company: ... Location: ... Apply: ..."
jobagent --profile private add --text-file posting.txt --title "AI Project Manager" --company "Acme"
jobagent --profile private add --batch saved_posts.txt
```

Supported ATS links are read through their APIs. Other links are followed where `robots.txt`
allows. LinkedIn, Indeed, WhatsApp and social media links are never opened: paste the text
instead. Add `--employer-text` only when the pasted text is copied from the employer's own
posting.

### Gmail setup for LinkedIn job alerts (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the
   **Gmail API**, configure the OAuth consent screen (External, add yourself as a test user),
   and create an **OAuth client ID** of type **Desktop app**.
2. Download the client JSON and save it as `data\private\gmail_client_secret.json`.
3. Install the Gmail extra and authorise read-only access in the browser (one time):

   ```powershell
   .venv\Scripts\pip install -e ".[gmail]"
   jobagent --profile private gmail auth
   ```

4. Set `intake.gmail.enabled: true` in `config.yaml`. LinkedIn alert senders are listed there
   too (`jobalerts-noreply@linkedin.com`, `jobs-noreply@linkedin.com`).

Each run reads new alert emails from the last 7 days, extracts title, company, location and
the LinkedIn link from the email itself, and looks for the same role on the company's own
Greenhouse, Lever, Ashby or Workable board. If found, the real advert is stored and the board
is added to `config/sources.yaml`. If not, the job is kept as a **LinkedIn lead** with its
LinkedIn link, for you to check by hand.

## Routing: how a job is decided

1. **Free rules (no AI).** Skip when the employer's own location field or text limits applicants
   to places without Kenya ("Remote - United States", "must be authorized to work in the US"),
   when stated pay is clearly below the floor (US$1,300/month part-time, US$1,800 full-time,
   hourly pay converted with the posting's own hours), or when the title is out of scope.
   LinkedIn leads go to LINKEDIN_LEAD. Scam-flagged jobs are capped at REVIEW.
2. **AI, only for what is left**, several jobs per call. The model returns a route, an
   eligibility verdict (KENYA_CONFIRMED, AFRICA_OR_WORLDWIDE_NO_EXCLUSION, UNCLEAR, EXCLUDED),
   the exact location wording it relied on, function and seniority fit, workplace, hours (US
   hours flagged) and reasons. Output is schema-validated, retried once, and on a second failure
   the job goes to REVIEW.
3. **Policy checks after the AI.** The quote must appear word for word in the employer's text,
   or the verdict drops to UNCLEAR. Aggregator, WhatsApp and pasted text can never prove
   eligibility. On-site roles and hybrid roles outside Nairobi and Nakuru are skipped. A
   location field that contradicts the description caps the job at CHECK_ELIGIBILITY.

### AI providers

Set `ai.provider` in `config.yaml`:

| Provider | Needs | Cost |
|---|---|---|
| `claude_code` (default) | Claude Code CLI logged in with your Claude subscription | Counts against your plan's usage limits; no per-call charge |
| `ollama` | [Ollama](https://ollama.com) running locally, `ollama pull qwen2.5:7b` | Free |
| `anthropic_api` | `pip install -e ".[api]"`, `ANTHROPIC_API_KEY` in `.env` | Paid per token (claude-opus-5-5: US$4 / US$20 per million input / output tokens); cost logged per call |
| `fake` | nothing | Free; keyword logic for CI and the demo, not a model |

Every call is logged in the `ai_calls` table (provider, model, latency, tokens). AI calls are
capped per run (`ai.max_calls_per_run`, default 60). If Claude reports a usage limit, the run
stops AI work, keeps the rule results, and the remaining jobs are routed next run.
`jobagent ai ping` checks the provider with one tiny call.

On real data (120 jobs) the rules settled 41 jobs for free; Claude routed the other 81 in 17
calls of about 13k input and 3k output tokens each.

## Sources

Configured in [config/sources.yaml](config/sources.yaml), with the check behind each entry.

**Not supported, deliberately:**

| Source | Why |
|---|---|
| Remotive | `robots.txt` disallows `/api/*` |
| BrighterMonday | No public RSS feed or API; `robots.txt` disallows `/api/` |
| Fuzu | Returns 403 to automated clients (bot protection is never bypassed) |
| We Work Remotely | API terms do not allow job search tools |
| LinkedIn, Indeed | No scraping. Use LinkedIn alert emails or `jobagent add` with pasted text |
| X (Twitter) | API is paid. Use `jobagent add` with pasted text |
| hiring.cafe | No automated access. Use it by hand to discover companies, then add their Greenhouse, Lever, Ashby or Workable board to `config/sources.yaml` |
| Reddit | Adapter built but off: `robots.txt` on Reddit's API host disallows all user agents |

## Development

```powershell
.venv\Scripts\ruff check .
.venv\Scripts\pytest
```

All network and AI calls are mocked in tests. CI runs on every push.
