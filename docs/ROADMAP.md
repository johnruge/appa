# Roadmap

Where appa is now, what ships first, and what it grows into. Each stage keeps
the two core rules: **the model reads and code decides**, and **only a job ID
blocks**.

## Where it stands (v0, local)

Working and tested (80 tests):
- Intake from links, screenshots and company boards (Greenhouse, Lever, Ashby), plus careers pages that embed them
- Filters for sponsorship, citizenship, clearance, experience and title, with evidence verification
- Application database (backfilled from your sheet), job-ID dedupe, CSV export
- Authenticated localhost API for your agents

Not yet run for real: model extraction and the eval (they need an `OPENROUTER_API_KEY`).

## Stage 1: open-source v0.1 (the first push)

Goal: someone can clone it, run it for themselves, and trust it.

- [ ] Run extraction on about 30 real postings you've already judged, and record precision and recall per rule (this doubles as the first eval set)
- [x] License: AGPL-3.0-or-later (`LICENSE`)
- [ ] `SECURITY.md` contact and `CONTRIBUTING.md`, including the contribution terms decision below
- [ ] Lockfile (`uv lock`), CI (tests, lint, `pip-audit`, secret scan)
- [x] PostgreSQL + Alembic migrations, least-privilege roles (M1)
- [ ] Clean-up: the `Job.ats_job_id` / `job_key` overlap; `resolve.py` reuses `normalize.title`

## Stage 2: always on, for you

- **Scheduler:** `poll` every few hours (launchd on your Mac, or a small VPS)
- **Email intake:** a dedicated Gmail for alerts (Gmail API, read-only scope); parse alert emails into links, then into the pipeline
- **Notifications and question queue:** Telegram bot. You get a daily digest of review items, it asks the questions it needs answered, and your answers are saved
- **Confirmation tracking:** "thanks for applying" emails mark rows Applied and fill `date_applied`

## Stage 3: auto-apply for straightforward applications (built in v1 M6: approval-gated)

- Playwright fillers for Greenhouse, Lever and Ashby (deterministic field mapping, not a free-roaming agent)
- **Dry run first:** fill, screenshot and wait for approval. A per-ATS switch to submit automatically once it has a track record
- Every answer comes from your approved profile or saved answers. Anything unknown goes to the question queue and is never invented
- Hard stops: essays, "are you human" or AI-detection questions, anything on your apply-myself list
- Workday later (per-company accounts, very varied forms)

## Stage 4: product (open core plus a hosted tier)

| | Open source (self-host) | Hosted (paid) |
|---|---|---|
| Intake, filters, DB, API, CLI | ✅ | ✅ |
| Your own API keys (Anthropic) | ✅ | included |
| Web interface (review queue, tracker, answers) | basic / none | ✅ |
| Always-on scheduling, email intake | DIY | ✅ |
| Auto-apply workers | run them yourself | ✅ managed, sandboxed |
| Support, multi-device | — | ✅ |

What the hosted tier requires is in [SECURITY.md](SECURITY.md#hosted-tier-requirements).
The architecture change: tenants, a job queue with workers (fetch / extract / apply),
Postgres with row-level security, and a web app talking to the same API.

### License: AGPL-3.0-or-later (decided)

Anyone may self-host or fork. Anyone who runs a modified version as a network
service must publish their changes under the AGPL, so nobody can turn your code
into a closed hosted competitor.

**One open decision before accepting outside contributions.** As the sole copyright
holder you can run the hosted tier however you like, including closed
hosted-only features. Code that *others* contribute stays theirs, under the AGPL,
unless they grant you more. Pick one before merging the first outside PR:

- **CLA** (contributor license agreement): contributors grant you the right to relicense. This keeps open-core and dual licensing possible. It's the most flexible for the business, but adds a little friction for contributors.
- **DCO** (`Signed-off-by` only): the lightest for contributors. The hosted tier then has to obey the AGPL for contributed code, so closed features go in separate, non-AGPL services that talk to appa over its API.

## Cost and reliability levers (after v0.1 measurements)

Measured first, then optimized:

1. **Free checks before paid ones:** seen, job ID and title filters already run before Claude. Adding location and remote filters is next.
2. **Cheaper model, gated by the eval:** `appa eval --model A --model B` compares models on the labeled set. Switch the default only if a cheaper model has no more false rejects or missed rejects.
3. **Batch API** for `poll` (about 50% cheaper, results within hours, fine for background polling).
4. **Truncate boilerplate** (benefits, EEO text) before extraction to cut input tokens.
5. **Prompt caching** once the system prompt and schema are large enough to cache.
6. **Reliability:** retries with backoff on feeds, a per-company circuit breaker, and alerts when a board's format changes (zero jobs parsed).
