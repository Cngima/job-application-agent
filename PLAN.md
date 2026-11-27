# job-application-agent: Build Plan

A personal job-search agent that finds, routes, scores, drafts and tracks remote job applications.
It never submits an application, never emails an employer and never signs in to a job site.
Carol reviews and submits everything.

Status key: `[ ]` not started, `[~]` in progress, `[x]` done and verified on real data.

---

## Ground rules (apply to every stage)

1. **No submitting.** No code path may submit a form, send mail to anyone other than the configured owner address, or authenticate to a job site. A test asserts that the digest sender refuses any recipient other than `OWNER_EMAIL`. (Reddit OAuth is the one login, and it is to Reddit's official Data API for reading, never to a job site.)
2. **Grounding.** Every generated CV, letter, answer or fit report passes `grounding.validate()` before it is saved. It rejects any tool, employer, title, number or credential that is not in the evidence bank, and anything on `never_claim`.
3. **Gaps stay gaps.** Fit reports list GAP requirements explicitly. Drafts never paper over them.
4. **Eligibility comes from the employer.** A job is eligible only if the employer's own posting names Kenya, lists Kenya, or says Africa-wide or worldwide with no exclusion. The exact quoted location text is always stored. Aggregator location labels are never trusted.
5. **Polite sourcing.** Official APIs, public ATS endpoints and public RSS only. robots.txt checked per host, each site's terms respected, per-host rate limit (default 1 request every 2 s), a `User-Agent` that names the project and repo URL. Never bypass bot protection or logins. Not used: LinkedIn, Indeed, We Work Remotely, X/Twitter automation.
6. **Privacy.** `.env` and `data/private/` are gitignored. The public repo ships `data/sample/` with a fictional person. CI fails if any file under `data/private/` is tracked. Before every push, `git status` is checked to confirm no private file is staged.
7. **No em dashes** in generated text or docs. The grounding validator and a lint test enforce it.

## Git and release workflow

- Author: `Carol Njogu <86587680+Cngima@users.noreply.github.com>` (set in the repo's local git config).
- Commit after each stage, push to `github.com/Cngima/job-application-agent` once that stage's tests and lint pass.
- Before the first push: `git status` and `git check-ignore` confirm `.env` and `data/private/` are ignored and not staged.

## Configuration files

| File | Holds | Tracked in git |
|---|---|---|
| `config.yaml` | System settings: AI provider, call caps, HTTP politeness, embedding model, paths | Yes |
| `config/search_profile.yaml` | Targeting: titles, priority keywords, seniority, exclusions, hours, pay floors, hourly conversion | Yes (Carol's real targets, nothing sensitive) |
| `config/sources.yaml` | Companies, boards, agencies, subreddits to watch | Yes |
| `.env` | API keys, SMTP, Reddit OAuth, API token | No |
| `data/private/evidence_bank.yaml` | Generated: Carol's facts, roles, allowed tools, never_claim, AI & automation projects | No |
| `data/private/evidence_overrides.yaml` | Carol's review decisions and owner-supplied facts, applied on every build | No |
| `config/drafting.yaml` | CV draft rules: contact links, projects section, part-time label, overlap limit | Yes |

### search_profile.yaml (seeded from Carol's brief)
- **Titles:** AI Project Manager, AI Implementation Specialist, AI Automation Specialist, Workflow Automation Specialist, Automation Project Manager, Technical Project Manager, Implementation Manager, Implementation Project Manager, AI Operations Specialist, Program Coordinator (AI or automation), Project Coordinator (automation or technical), Revenue/Marketing Operations (automation focus).
- **Priority keywords:** AI, automation, agents, Claude, n8n, Make, Zapier, workflow, integrations, APIs, HubSpot, client onboarding, implementation.
- **Seniority:** mid-level and senior. Skip internships, entry-level, director and above.
- **Exclude:** pure software engineering, data science or ML engineering, sales/SDR/BDR quota roles, call centre or customer support, roles requiring a licence Carol does not hold.
- **Hours:** full-time or part-time. US working hours acceptable but always shown in the digest. Part-time and flexible roles get a small score bonus.
- **Pay floors:** US$1,300/month part-time, US$1,800/month full-time.
- **Job age:** postings older than 7 days are never stored (`max_age_days: 7`). Undated postings are kept and flagged `date_unknown`.
- **Hourly conversion:** use the posting's stated hours when given, otherwise 80 hours/month part-time and 160 full-time. A converted figure is an estimate, so anything up to 10% below the floor routes to REVIEW, not SKIP.

## Environment findings (2026-11-24)

| Item | Finding | Impact |
|---|---|---|
| Python | 3.14.4 installed (no 3.11) | Target `>=3.11`; CI tests 3.11 and 3.12 |
| Claude Code CLI | 2.1.331 present | Default provider works with no key |
| Ollama | Not installed | Fallback provider coded and tested with mocks; Carol installs Ollama to use it for real |
| `gh` CLI | Not installed | Push over HTTPS with git only |
| CVs | 39 files match `Carol Njogu_CV_*.docx`, consistent layout | Stage 1 parser is rule-based (no AI) |
| Job Hunt Log | 34 rows, most links are aggregator pages, posting text not stored | Eval set needs employer postings and top-ups (see Stage 3b) |

## Tech choices

| Concern | Choice | Why |
|---|---|---|
| Package | `src/jobagent/`, CLI via `typer`, entry point `jobagent` | Standard layout, testable |
| Models | `pydantic` v2 | Validated AI JSON, config, Job model |
| HTTP | `httpx` + `tenacity` (retry with backoff) | Timeouts, retries, easy mocking with `respx` |
| DB | `sqlite3` with numbered SQL migrations | No ORM to explain, migrations are plain SQL |
| Embeddings | `fastembed` with `BAAI/bge-small-en-v1.5`, float32 BLOBs in SQLite, cosine in numpy | Free, local, no vector DB needed |
| AI | `ai/provider.py` interface: `claude_code`, `ollama`, `anthropic_api`, `fake` | One switch in `config.yaml` |
| Dashboard | Streamlit, root `streamlit_app.py`, new SQLite connection per session | Same pattern as automation-metrics-dashboard |
| MCP | Official `mcp` Python SDK (FastMCP) | Claude Desktop integration |
| API for n8n | FastAPI on localhost, token in `.env` | Small surface for the n8n trigger |
| Quality | `pytest`, `respx`, `ruff`, GitHub Actions | Required quality bar |

---

## Milestones

### Stage 0: Scaffold `[x]`
Repo layout, `pyproject.toml`, `.gitignore`, `config.yaml`, `.env.example`, logging, ruff and pytest config, CI workflow with a private-data guard.
**How to test:** `pip install -e .[dev]`, then `jobagent --help`, `ruff check .`, `pytest`.
**Result:** 3 tests pass, ruff clean.

### Stage 1: Evidence bank `[x]`
- `jobagent evidence build --cv-dir "<Clients CV>"` parses every `Carol Njogu_CV_*.docx`, splits each into facts (one bullet = one fact), attaches role, employer, dates and source files, extracts tools against a known-tools vocabulary and metrics by regex.
- The same fact phrased slightly differently across CVs is merged and keeps a list of all source files.
- Writes `data/private/evidence_bank.yaml`: `facts`, `allowed_tools` (only tools found in the CVs), `credentials`, `never_claim` (seeded: ERP, Codex, ClickUp as deep configuration, CAT tools, DNS, procurement-to-pay, RICE, Kano and similar product frameworks), `preferences` (remote only, part-time OK, timezone UTC+3, based in Kenya). Targeting lives in `search_profile.yaml`, not here.
- Writes `data/sample/evidence_bank.yaml` for a fictional person with invented employers.
- `jobagent evidence check` validates a bank against the pydantic schema.

**How to test:** run the build, spot-check 10 facts against their source CV, confirm every `allowed_tools` entry appears in a CV. **Carol reviews and corrects the YAML before any later stage uses it.**

**Result (2026-11-24):** done and reviewed by Carol.
- Review decisions are stored in `data/private/evidence_overrides.yaml` and applied on every build, so the reviewed bank is reproducible from the CVs and a rebuild never loses the review.
- After review: 476 facts, 9 roles with approved alternate titles, 9 credentials, 63 tools, 10+ years of experience (20 conflicting profile sentences dropped).
- 39 CVs parsed into 495 facts (305 experience, 174 profile sentences, 10 project, 6 achievement), 9 roles, 63 allowed tools, 24 credential variants.
- All 495 facts verified to appear word for word in at least one cited source CV. Zero em dashes in the bank.
- `data/private/evidence_bank_review.md` lists what needs a human decision: retitled roles, years-of-experience conflicts, rarely-claimed tools, credential variants, and single-CV facts.
- The build never overwrites a reviewed bank: a rebuild writes `evidence_bank.new.yaml` unless `--force` is given.
- Tool matching is case-sensitive so "zoom in" or "word" never counts as a tool claim.
- Second review (2026-11-24): Lynn Resumes added as a part-time role (Founder & Lead CV Writer, 2022 to present, 6 owner-supplied facts with their own dates so 2026 AI work is never backdated, plus drafting constraints). AI & AUTOMATION PROJECTS section added (3 ready, job-application-agent pending until built). GitHub added to contact. Location fixed to Nairobi, Kenya. Python and Streamlit added as owner-named tools.
- 21 Stage 1 tests (parser, builder with generated .docx files, never_claim patterns, overwrite protection, sample bank).

### Stage 2: Sourcing `[x]`
Shared `Source` interface `fetch() -> list[Job]`. Every Job records `source`, `source_kind` (`employer_ats`, `discovery`, `agency`, `community`, `manual`) and the URL of the employer's own posting when known.

| Adapter | Endpoint | Kind | Notes |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` | employer_ats | |
| Lever | `api.lever.co/v0/postings/<company>?mode=json` | employer_ats | |
| Ashby | `api.ashbyhq.com/posting-api/job-board/<slug>` | employer_ats | |
| Workable | `apply.workable.com/api/v1/widget/accounts/<slug>` | employer_ats | |
| BreezyHR | `<company>.breezy.hr/json` | agency / employer_ats | Seed: Bruntwork, Pavago and Log agencies, where they use Breezy |
| Zoho Recruit | Public careers-site feed, if one exists without login | agency | Verify in Stage 2; skip and document if none |
| Remotive | Public API | discovery | Follow its rate guidance and attribution terms |
| RemoteOK | Public API | discovery | Attribution and link-back required by its terms |
| Himalayas | Public API | discovery | Discovery only |
| Jobicy | Public API | discovery | Discovery only |
| Reddit | Official Data API, OAuth "script" app, creds in `.env` | community | r/forhire, r/RemoteJobs, r/hiring, r/WorkOnline, `[Hiring]` posts only |
| Manual | `jobagent add <url or pasted text>` | manual | For X, WhatsApp, LinkedIn, hiring.cafe and anything else |

- **Discovery sources** (Remotive, RemoteOK, Himalayas, Jobicy): their location labels are ignored. The agent resolves the employer's own posting (by following the apply link to a Greenhouse, Lever, Ashby, Workable or Breezy page when it is one) and reads eligibility from that text. If no employer posting can be read, the job routes to CHECK_ELIGIBILITY.
- **Reddit:** never routed to APPLY, only REVIEW or CHECK_ELIGIBILITY. Posts that ask for payment, or that move the conversation to Telegram or WhatsApp, are flagged `scam_risk`. If Reddit credentials are missing or the API refuses access, the source is skipped and logged and the run continues.
- **Kenyan boards** (BrighterMonday, MyJobMag, Fuzu): included only if each offers a public RSS feed or API and its robots.txt and terms allow automated access. Checked in Stage 2. Otherwise listed in the README as not supported.
- **hiring.cafe:** no automated access. README explains the manual workflow: discover companies there, then add their ATS boards to `sources.yaml`.
- **Manual intake:** `jobagent add` accepts a URL or pasted text. If the URL is a supported ATS page it is fetched through the polite client. Otherwise the pasted text is used as is. Same routing, fit scoring and tracking, with `source = manual`.
- **Age limit (Carol, 2026-11-24): jobs must not be older than a week.** `max_age_days: 7` in `search_profile.yaml`. Checked before any detail request where the board's list carries a date (Workable, Breezy, MyJobMag), and again for every job before storing. Greenhouse uses `first_published`, not `updated_at`. Undated postings are kept and flagged `date_unknown`. `jobagent prune` removes stored jobs past the limit and backs up the database first.
- Normalise to one `Job` model, de-duplicate by canonical URL and by normalised `title + company`, skip anything already stored.
- `PoliteClient`: robots.txt cache, per-host rate limit, identifying User-Agent, retry with backoff.
- Seed `sources.yaml` from the Job Hunt Log. Companies with no permitted feed (for example Safaricom on Oracle, Talent Hackers on ApplyToJob) are listed as `manual_watch` with a note.

**How to test:** unit tests with recorded JSON fixtures per adapter, including Reddit scam flags. Live: `jobagent source --dry-run` prints counts per source and sample titles and writes nothing.

**Result (2026-11-24):** done, run twice on real data.
- Every source was checked live before coding: board owner name, response shape, robots.txt and terms. Findings are recorded in `config/sources.yaml`.
- Enabled: GitLab, pawaPay, Invisible Technologies (Greenhouse); AJAIA, Pavago (Workable); 360dialog, Social Discovery Group (Breezy); Bruntwork (Zoho Recruit); Scale Army, Hire Hangar (Ashby); RemoteOK, Himalayas (`country=KE` search), Jobicy, MyJobMag Kenya RSS.
- Not supported, with reasons: Remotive (robots.txt disallows `/api/*`), BrighterMonday (no feed, `/api/` disallowed), Fuzu (403 bot protection, never bypassed), We Work Remotely, LinkedIn, Indeed, X, hiring.cafe. Lever adapter is built and tested but no Job Hunt Log company uses Lever.
- 16 companies with no readable board are on a `manual_watch` list (Talent Hackers on JazzHR, Safaricom on Oracle, Pulitzer Center on Rippling, Remote.com whose Greenhouse board is empty, and others).
- Board-name checks matter: Greenhouse `remote` is General Assembly, Greenhouse `insight` is not Insight Enterprises. The resolver rejects such collisions (tested).
- First real run: 194 new jobs from 15 sources in 5m14s, 0 errors. Second run: 60 new (Pavago backlog), 3m30s; stored postings cost no detail requests.
- Database after two runs: 254 jobs, 159 with employer text (8 of them discovery leads traced to the employer), 95 discovery leads with aggregator text only, 0 empty descriptions.
- Design decisions found on real data:
  - De-duplication: same URL always; same title and company only across different sources, because employers such as AJAIA post one title per country and only some of those may be open to Kenya.
  - Detail fetches are gated: a posting already stored or already seen this run is skipped before any request, so Pavago's 60-fetch cap always goes to new postings (about 2,600 listings are covered over a few daily runs).
  - Resolver company lookups are capped at 15 per run and cached (90 days positive, 30 days negative). Following an ATS apply link is not capped. **Moved to Stage 3:** look up only discovery jobs that survive the free rules, so no requests are spent on jobs that will be skipped anyway.
- 49 Stage 2 tests (76 in total): politeness (robots allow/deny/5xx, per-host wait, Retry-After, User-Agent), every adapter, URL matching, resolver (apply-link, name collision, cache, cap), de-duplication, dry run writes nothing, a failing source never stops the run, manual intake never fetches unsupported sites.

### Intake: WhatsApp inbox, manual jobs, LinkedIn alerts, scam checks `[x]`
Added 2026-11-24 at Carol's request.
- **WhatsApp (no WhatsApp access of any kind):** Carol saves copied or exported channel posts as `.txt` in `inbox/` (gitignored; desktop shortcut via `scripts/create_inbox_shortcut.ps1`). `jobagent inbox` splits files into posts (chat-export timestamps, dash separators or blank lines), drops chatter, de-duplicates (by content and by link), follows up to 2 apply links per post, runs scam checks and stores jobs with source `whatsapp`. Processed files move to `inbox/processed/`; identical content is never processed twice. `jobagent add --batch <file>` does the same immediately.
- **Following links:** GET only. Redirects followed by hand so `robots.txt` is checked on every hop. LinkedIn, Indeed, WhatsApp, Telegram and social media are never opened. Supported ATS links use their APIs. Other pages: schema.org JobPosting if present; text counts as the employer's only on an ATS or the hiring organisation's own domain.
- **`jobagent add <url or text>`:** pasted text is parsed for title, company and location when not given, and gets scam checks.
- **LinkedIn job alert emails:** Gmail with `gmail.readonly` only (`jobagent gmail auth`, then `jobagent gmail alerts`). Parses the plain-text part of the email; never fetches linkedin.com. Each lead is traced to the company's own Greenhouse, Lever, Ashby or Workable board; if found, the real advert is stored and the board is appended to `config/sources.yaml` (comments preserved, file validated, restored on error). If not, the job is stored as a `linkedin_lead` with the clean LinkedIn link. Leads skipped because of the lookup cap are flagged `lookup_pending` and retried next run. Processed messages are remembered in `gmail_messages`.
- **Scam checks** (WhatsApp, manual, Reddit): fee or M-Pesa payment, only a personal phone or WhatsApp number, free email address for a named company, unrealistic pay, ID or bank documents up front. Reasons stored in `risk_reasons`.
- **Real-data check:** Carol's 22 Nov LinkedIn alert (6 jobs): 1 off-target title dropped, Thunes traced to its Greenhouse posting and its board added to `sources.yaml`, 4 kept as LinkedIn leads (no public board), 0 requests to linkedin.com.
- **Not yet verified live:** the Gmail API path itself, because it needs Carol's one-time Google OAuth setup (README, "Gmail setup"). It is tested with a fake Gmail client.
- 37 intake tests (117 in total).

### Stage 3: Decision routing `[x]`
Additional rules from Carol (2026-11-24), to build in this stage:
- **Location** (`search_profile.yaml`): remote roles always in scope; hybrid only in `kenya_hybrid_cities` (Nairobi, Nakuru); fully on-site roles skipped while `kenya_onsite_ok: false`.
- **Scam-flagged jobs** route to REVIEW with their reasons, never APPLY.
- **`linkedin_lead` jobs** route to **LINKEDIN_LEAD** (a fifth route, no AI call) and are listed in the daily digest with the LinkedIn link.
- **WhatsApp, pasted and Reddit text** can never prove eligibility on its own: at most REVIEW or CHECK_ELIGIBILITY unless the employer's own advert was found.
- **3a Rules (free):**
  - Location exclusion parser over the employer's own text only ("US only", "EMEA excluding Africa", "LATAM only" and similar). Skip on clear exclusion of Kenya.
  - Pay floor from `search_profile.yaml`: stated hours first, else 80 or 160 hours/month. Below floor by more than 10%: SKIP. Within 10% below: REVIEW. Range: compared on its top figure.
  - Out-of-scope functions and seniority (exclusion list in `search_profile.yaml`).
  - (Replaced: the 60-day evergreen flag is no longer needed because postings older than 7 days are dropped at sourcing.)
  - Source caps: Reddit at most REVIEW. Discovery sources with no employer text at most CHECK_ELIGIBILITY.
- **3b AI** for jobs that pass: batched (default 5 per call), returns per job `route`, `eligibility_verdict` (KENYA_CONFIRMED / AFRICA_OR_WORLDWIDE_NO_EXCLUSION / UNCLEAR / EXCLUDED), `quoted_location_text`, `function_match`, `seniority_match`, `hours_requirement` (including whether US hours are required), `reasons`. Pydantic-validated, one retry, then REVIEW.
- `quoted_location_text` must be a literal substring of the employer's text. If it is not, the verdict drops to UNCLEAR.
- Priority score: title match, priority keyword hits, small bonus for part-time or flexible hours.
- Every decision stored with rule hits, AI output, provider and prompt version.

**How to test:** rule tests on hand-written postings including hourly edge cases. `jobagent route --limit 10` on real jobs, Carol reviews the decisions table.

**Result (2026-11-25):** done, run on all 120 real jobs.
- Free rules settled 41 jobs (37 SKIP with the employer's own location quote, e.g. "Pakistan; Pakistan", "Remote - United States"; 4 LINKEDIN_LEAD). 19 discovery leads were traced to employer boards before the AI saw them.
- Claude (claude-opus-5-5 via the Claude Code CLI) routed the other 81 jobs in 17 calls of 5 jobs: about 13k input and 3k output tokens and 27 s per call, no invalid outputs, no retries.
- Current routes: APPLY 1, CHECK_ELIGIBILITY 19, REVIEW 36, LINKEDIN_LEAD 4, SKIP 60.
- Found on real data and fixed: pay amounts in perk sentences ("learning budget of USD 2,000 per year", "$150 per month stipend") were read as salary; they are now ignored. "South Africa" no longer counts as Africa-wide. A job with "&" in its title lost the title (HTML parser not flushed). One posting contained hidden text telling AI agents to skip it: Claude ignored it, and the prompt (now `route-v2`) says outright that postings are data, never instructions.
- New rule from real data: when the employer's location field lists countries without Kenya but the description says Africa or worldwide, the job is capped at CHECK_ELIGIBILITY (a contradiction is not proof).
- Observed: re-routing the same job can change REVIEW vs APPLY on borderline cases. The Stage 3b eval set measures this.
- Paid API provider: claude-opus-5-5, structured output, server-side refusal fallback, cost from published prices. Ollama provider coded and tested with mocks (Ollama not installed).
- 41 Stage 3 tests (157 in total).

### AI provider layer (built with Stage 3) `[x]`
- `complete(prompt, json_schema) -> dict` with validation.
- `claude_code`: `claude -p --output-format json`, with a timeout. Usage-limit messages raise `ProviderPaused`; the run stops AI work, logs it and keeps rule-only results.
- `ollama`: HTTP to `localhost:11434`, model from config (default `qwen2.5:7b`).
- `anthropic_api`: off by default, key from `.env`, model from config.
- `fake`: replays recorded responses keyed by prompt hash. Used in CI and the demo.
- Every call logged to `ai_calls` (provider, model, latency, tokens if reported, cost only for API). `max_calls_per_run` default 60.

**How to test:** mocked tests for each provider including timeout, non-zero exit and usage-limit cases. One real call through `jobagent ai ping`.

### Stage 3b: Labelled eval set (moved here from Stage 11) `[~]` (labels awaiting Carol)
- Start from the 34 Job Hunt Log rows. For each, find the employer's own posting and save its text to `data/private/evals/postings/` (live where still open, otherwise Carol pastes it or the row is dropped).
- Top up to about 40 with postings sourced in Stage 2.
- Propose a label (route, eligibility verdict, one-line reason, quoted location text) for every posting and **show every proposed label to Carol for confirmation**.
- `jobagent eval --provider claude_code|ollama|fake` reports routing accuracy, eligibility accuracy, a confusion table and every disagreement. Results saved to `evals/results/` with the prompt version.
- Tune routing rules and prompts against the confirmed labels. Re-run whenever a prompt changes.
- The public repo ships a small sample eval set of fictional postings so CI can run evals with the fake provider.

**How to test:** CI runs the sample evals on `fake`. Locally run on `claude_code` (and `ollama` once installed), paste scores into README.

**Progress (2026-11-27):**
- Only 2 of the 34 Job Hunt Log postings are still live on an employer board (AJAIA, Hire Hangar); the log stores no posting text, so the other 32 cannot be labelled unless Carol has the adverts saved.
- Private set: 40 frozen postings in `data/private/evals/postings.jsonl` (2 from the log plus 38 sourced in Stage 2, chosen to cover every route, every source type and the hard cases: location conflicts, EMEA, aggregator labels, Nairobi with no workplace stated, a prompt-injection posting). Text is frozen so the set stays reproducible after adverts close.
- Proposed labels in `data/private/evals/labels.yaml`, written from each posting's own text, 3 marked JUDGEMENT CALL. Only labels Carol confirms are scored.
- `jobagent eval [--provider X] [--repeat N]` runs the full router in a throwaway database and reports routing and eligibility accuracy, a confusion table, every disagreement, AI calls and tokens, and (with `--repeat`) how many jobs change route between runs. Reports saved to `<data>/evals/results/`.
- Public sample set of 8 fictional postings for CI (`data/sample/evals/`), run with the fake provider.
- Resolver fix found while building the set: boards already in `sources.yaml` are checked first (Scale Army -> Ashby "Scale Army Careers").

### Stage 4: Fit scoring with RAG `[ ]`
- Embed evidence facts with fastembed, store in `fact_embeddings`.
- Extract requirements from each APPLY/REVIEW job (batched).
- For each requirement, retrieve top-k facts (default 4), send only those facts and the requirement to the AI, get `MET` (with fact ids), `PARTIAL` or `GAP`. Fact ids are checked to exist.
- Fit report: score 0 to 100 (MET 1.0, PARTIAL 0.5, GAP 0; must-haves weighted double), matched requirements with evidence, gaps, recommended CV angle. Passes the grounding validator.
- APPLY jobs at or above `shortlist_threshold` (default 70) become `shortlisted` applications.

**How to test:** retrieval tests with the sample bank. Live on 5 real jobs; Carol judges whether the MET/GAP calls are honest.

### Stage 5: Tailored drafts (on command only) `[ ]`
Draft rules from Carol (`config/drafting.yaml`):
- Contact line: Nairobi, Kenya, with clickable LinkedIn and GitHub links.
- "AI & AUTOMATION PROJECTS" section after Professional Experience, cv-builder-qa `diamond` type (bold title, one description line with a clickable link). Pick 2 or 3 most relevant projects per job, all of them for AI automation roles. Pending projects never appear.
- Part-time roles labelled as part-time. Fact dates kept (Lynn Resumes AI work shown as 2026, never since 2022).
- Overlap check: a draft is rejected if a role bullet and a project line share 8 or more consecutive words.
- The job-application-agent project entry is added only after this repo is built, tested and pushed, describing only working stages.

- Lynn Resumes short versions (approved 2026-11-24): when `proj-lynn-agents` or `proj-metrics-dashboard` is in the CV, the matching role bullet uses its `short_text` with no date shown. Otherwise the full bullet is used.
- Assent 2017-10 role: always titled "Customer Support Coordinator (Supplier Support)", framed as customer support for the platform's supplier users (phone, email, live chat, Zendesk; CSAT and QA scores), led by fact `assent-dd917a`. The words "procurement" and "supplier management" are banned for this role; the validator rejects a draft that uses them there.

**First step of Stage 5: link support in cv-builder-qa (approved by Carol 2026-11-24).** Its only hyperlink today is the hard-coded LinkedIn link in the contact line. In github.com/Cngima/cv-builder-qa:
1. Add a generic link field (text + URL) for contact-line items and for `diamond` items.
2. Keep the existing `linkedin` field working unchanged.
3. Tests for both link types and for LinkedIn backwards compatibility; update the README and JSON Schema.
4. Push as a normal commit to that repo, then use it here for the GitHub contact link and project links.

- `jobagent draft <job_id>` only for jobs Carol approves.
- Install `cv-builder-qa` from GitHub, read its JSON Schema, build `cv_spec.json` by selecting and ordering real facts (text from the evidence bank, no new claims).
- Grounding validator, then `cvbuilder build`, `cvbuilder qa`, `cvbuilder ats`. Draft and `report.md` saved under `data/private/drafts/<job_id>/`.
- `jobagent answer <job_id>` drafts grounded answers to pasted application questions.

**How to test:** draft one real job, check the QA and ATS reports, confirm a deliberately ungrounded spec is rejected.

### Stage 6: Tracking `[ ]`
Tables: `sources`, `jobs`, `decisions`, `fit_reports`, `applications`, `events`, `agent_runs`, `ai_calls`, `fact_embeddings`, `schema_version`.
CLI: `jobagent applied <job_id>`, `jobagent status <job_id> <status>`, `jobagent list`.
Imports the existing Job Hunt Log so history is kept.
Optional `jobagent gmail sync` (`gmail.readonly`): matches replies to applications and **proposes** status changes for Carol to confirm.

**How to test:** migration tests on empty and existing DBs, CLI tests, Gmail matcher on fixture messages.

### Stage 7: Daily run and digest `[ ]`
- `jobagent run` does stages 2 to 4, writes an `agent_runs` row, prints an error summary. A failing source never fails the run.
- Digest: top shortlisted jobs with score, route, quoted eligibility text, hours (US hours flagged), link, gaps. SMTP to `OWNER_EMAIL` only, or written to `data/private/digests/` if SMTP is not set.
- `docs/SCHEDULING.md` for Windows Task Scheduler. `.github/workflows/demo.yml` runs the sample profile with the fake provider.

**How to test:** one manual run on real data, check the digest, then one scheduled run.

### Stage 8: Dashboard `[ ]`
Funnel, routing breakdown, source performance (eligible jobs per source), response rate, run health, AI usage per run and month, estimated time saved (assumption documented, default 6 minutes manual review per screened job). Runs on sample data by default.

**How to test:** `streamlit run streamlit_app.py` on sample and private DBs.

### Stage 9: MCP server `[ ]`
Read-only `search_jobs`, `get_job`, `get_fit_report`, `pipeline_summary`, one write tool `create_draft`. No submit or delete tools exist.

**How to test:** in-process MCP client tests, then a live question from Claude Desktop.

### Stage 10: n8n workflow `[ ]`
FastAPI `POST /run` (token-protected, localhost) and `GET /digest`. `workflows/daily_digest.n8n.json` with schedule trigger, HTTP call and email node. Setup notes in `docs/N8N.md`.

**How to test:** FastAPI TestClient tests, then import into n8n and run once.

### Final: Documentation `[ ]`
README (mermaid architecture, routing versus RAG, setup, sources supported and not supported with reasons, cost notes, limitations, latest eval scores), `docs/HOW_IT_WORKS.md` in plain English. Every README claim maps to a built and tested feature.

---

## Open questions for Carol

1. **Reddit:** robots.txt on both `oauth.reddit.com` and `www.reddit.com` disallows all user agents, which conflicts with the "respect robots.txt" rule even for the official API. Options: keep Reddit off (current), or record an explicit exception that robots.txt targets crawlers and the authenticated Data API is governed by Reddit's API terms. Reddit may also require approval before a new API app works.

Resolved earlier: Resolved 2026-11-24: cv-builder-qa link support (yes, at Stage 5), Lynn Resumes short versions (approved), titles (below).

Resolved titles: **Two titles not found in any CV:** "Lead Project Coordinator (Customer Success Operations Department)" (CVs say "Team Lead, Project Coordinator (...)") and "Supplier (Customer) Support Coordinator" / "Customer Support Coordinator" (CVs say "Supplier Support Coordinator"). Carol confirmed "Lead Project Coordinator (Customer Success Operations Department)" (without "Team") and set the Assent support role to "Customer Support Coordinator (Supplier Support)" for all drafts.

## Progress log

- 2026-11-24: Plan written. Environment checked. Stage 0 scaffold done (3 tests pass, ruff clean).
- 2026-11-24: Plan updated with Carol's answers: noreply commit email, push per stage, evals moved after Stage 3, hourly conversion with 10% REVIEW band, search profile, expanded sources (Himalayas, Jobicy, BreezyHR, Zoho Recruit, Reddit, manual intake), unsupported sources documented. Stage 1 started.
- 2026-11-24: Stage 1 evidence bank builder done and tested (20 tests). Real bank built from 39 CVs, awaiting Carol's review.
- 2026-11-24: Carol's review applied through an overrides file. Stage 1 complete (22 tests).
- 2026-11-24: Full-time floor US$1,800/month. Lynn Resumes role, AI & automation projects, GitHub contact and drafting rules added (24 tests).
- 2026-11-24: Short versions, support-role title and framing rules applied. Parser fix: "Earlier roles:" summary lines are no longer filed as bullets of the role above (27 tests). 474/474 CV facts still verbatim in a cited CV.
- 2026-11-24: Stage 2 sourcing done: 15 sources live, 254 real jobs stored, 76 tests. Reddit off pending Carol's decision.
- 2026-11-27: Eval runner, 40-posting private set and proposed labels built; waiting for Carol to confirm labels before scoring Claude. 163 tests.
- 2026-11-25: Stage 3 routing done: rules, AI provider layer (Claude Code, Ollama, Anthropic API, fake), policy checks after the AI, decisions table, `jobagent route` / `decisions` / `ai ping`. All 120 real jobs routed. 157 tests.
- 2026-11-24: Intake added: WhatsApp inbox and batches, `add <url or text>`, LinkedIn alert emails via read-only Gmail, scam checks, location settings in the search profile. Desktop shortcut created. README started (describes built features only). 117 tests.
- 2026-11-24: 7-day age limit added (Carol). Pruned 140 stale jobs (backup kept), re-ran: 124 jobs stored, oldest 6 days, run time down from 5m14s to 1m23s because stale postings cost no detail requests; Pavago's 2,616 listings now covered in one run. 80 tests.
