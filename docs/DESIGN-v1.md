# appa v1 design

**Status:** proposal for review. Nothing in here is built yet.
Today's system is described in [ARCHITECTURE.md](ARCHITECTURE.md). This document is what it becomes.

## Goals

1. A **deployed backend** you control. You start agents from a terminal-style web UI or the CLI, choose the model per run, and schedule runs.
2. **Agents with tools:** v1 searches, screens, takes your screenshots, and **applies in your real browser on your Mac**, with dry-run review first and gated submit.
3. **Any model through OpenRouter,** with every run recorded so models can be compared on cost and accuracy.
4. **Secure by design:** secure enough to host for yourself now, and structured so a multi-user hosted tier is additive, not a rewrite.
5. **Cheap:** one small server, free checks before paid ones, and model choice per task.

Not in v1: email intake (v2), multiple users (hosted tier).

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Shape | Backend service. The CLI and web UI are thin clients of one API | Deployable, forkable, hostable; one source of truth |
| Backend | Python (existing code, FastAPI, SQLAlchemy) | Keeps working code; Playwright and agent tooling are strong in Python |
| Frontend | TypeScript + React (Vite), a static build served by Caddy on the same domain | Live terminal-style UI; same origin, so no CORS |
| Database | **Postgres everywhere** (local via `scripts/dev-db.sh`, prod on the VPS in Docker), Alembic migrations | One engine to reason about; it also serves as the job queue |
| Job queue | A `jobs` table claimed with `SELECT … FOR UPDATE SKIP LOCKED`, with leases and retries | No Redis to pay for or secure; about 150 lines we fully understand |
| Live output | `run_events` table + Postgres `LISTEN/NOTIFY` → Server-Sent Events to the UI | A streaming "terminal" without websockets or extra infrastructure |
| Models | **OpenRouter** through an OpenAI-compatible adapter | One key, hundreds of models; Ollama, OpenAI or native Anthropic later by configuration |
| Hard rules | Structured policy (typed, validated), applied by code | Can't be argued away by a manipulated posting; testable |
| Judgment | `profile.md`: plain-language instructions read by the Screener | "Big tech / big in software → I apply myself" is better as a description than a list |
| Browser (v1) | **Local runner on your Mac** that pulls apply jobs over HTTPS and drives a dedicated Chrome profile with Playwright | Real browser, home connection, nothing on your Mac exposed |
| Deploy | **One small VPS:** Docker Compose with Caddy, api, worker, scheduler and postgres | About $5–10/month; only 443 is public |

## Components

```
Internet ──443──► Caddy ─┬─ /            → web/ (static React build)
                         └─ /api/*       → api:8000   (private network only)

api        FastAPI: auth, applications, profile, runs, schedules, uploads, SSE stream
worker     claims jobs from Postgres, runs agent roles, writes run_events
scheduler  every minute: due schedules → enqueue runs
postgres   data + queue; no published port
runner     on your Mac: polls /api/runner/next, drives Playwright in a dedicated Chrome profile, reports back
```

The four server services share a private Docker network, and only Caddy publishes a port. The runner is not on the server: it only makes outbound HTTPS calls to `/api/runner/*`.

## Agents

An agent role is **instructions + tools + a default model + budgets**. Starting one creates a **run**.

| Role | v1 | What it does | Tools | Style |
|---|---|---|---|---|
| Scout | ✅ | Poll boards, take links, read screenshots, find the real posting | `boards.list`, `fetch_posting` (guarded), `screenshot.read`, `resolve`, `seen` | Workflow; agentic search for new companies later |
| Screener | ✅ | Extract facts, apply hard rules, decide whether you apply yourself or it goes to auto | model + rules (code) | Workflow |
| Concierge | ✅ | Collect open questions and review items for you | `ask_user` | Workflow |
| Applier | ✅ | Fill and (gated) submit applications in your browser | `browser.*`, `answers.lookup`, `resume.attach`, `ask_user`, `submit` (gated) | Deterministic fillers for known ATSs, agent fallback for odd fields |

**Workflow vs agent:** full model-driven loops are only for open-ended work. Everything else is code with model calls inside it, which makes it cheaper, testable and predictable.

### Run lifecycle

```
queued ─► running ─► succeeded
              │  └─► failed (error recorded)
              ├─► waiting_for_you (ask_user) ─► running
              └─► cancelled (you, or a budget hit)
```

Each run records: role, model, input, **budgets** (max dollars, max steps, max model calls), every model call (tokens and cost from OpenRouter's usage report), every tool call, and the final result. The worker checks for cancellation and budget between steps.

### "Apply myself" routing (`profile.md`)

The Screener gets your profile and returns `route: auto | manual` with a one-line reason. Overrides always win:

```markdown
## Apply myself
Queue for me instead of auto-applying:
- Big tech: FAANG+ (Google, Meta, Apple, Amazon, Netflix, Microsoft, Nvidia…)
- Companies that are big in software even if not household names (Datadog, HubSpot,
  Stripe, Snowflake, Databricks…)
- Startups where I'm unusually well positioned: AI/agent tooling, dev tools, …

## Always auto
- …

## Never apply
- …
```

A misrouted job only means it lands in your queue, or is auto-applied when you'd have done it yourself, so a model making this call is acceptable. Hard rules (sponsorship, citizenship, clearance, years) never go through the model's judgment.

## Applying in your browser (Applier + runner)

### How a job gets applied

```
Screener: route=auto ──► apply job queued ──► runner (your Mac) claims it
                                                   │
             ┌─────────────────────────────────────┘
             ▼
  open posting in dedicated Chrome profile ─► detect ATS ─► map every field:
     known field  → answer from your answers bank / profile / résumé
     unknown field, essay, "are you human/AI", CAPTCHA → ask_user, run pauses
             ▼
  all fields filled ─► screenshot + filled-values summary ─► DRY RUN: waits for your OK
             ▼                                                (in the UI: approve / fix / skip)
  submit (gated tool) ─► confirmation page detected ─► record_application(job_key, Applied)
```

### Runner design
- **A small Python program on your Mac** (`appa runner`), authenticated with a runner-only API key (scope `runner`). It **polls** `/api/runner/next` over HTTPS, so there are no inbound connections and nothing on your Mac is exposed.
- **Dedicated Chrome profile** (`~/Library/Application Support/appa/chrome-profile`), separate from your personal browsing. Logged-in ATS sessions persist there. It runs visible, not headless, so you can watch or take over at any point.
- **One application at a time,** with human-paced timing between actions. It works only while your Mac is awake and the runner is on; queued jobs wait.
- **What the runner sends back:** field-by-field results, screenshots (stored like uploads) and the final status. Your résumé file lives on the runner machine, and the server only stores its hash and a parsed text version for answering.

### Filling strategy
1. **Deterministic fillers per ATS** for **Greenhouse, Lever and Ashby** first, since their forms are consistent. Standard fields (name, email, phone, links, résumé, work authorization, EEO) are mapped by code from your answers bank. This step is cheap and predictable.
2. **Model fallback for unrecognized fields:** the model sees the field label and options, plus your answers bank, and either picks an answer **that exists in your bank** or returns `ask_user`. It never invents facts.
3. **Workday, iCIMS and others:** v1 opens them and queues them for you ("apply myself, prefilled where possible"). Full support comes later.

### Answers bank
Your facts live in an `answers` table, keyed by normalized question: contact info, links, graduation date, work authorization ("authorized now; will require sponsorship in the future"), willingness to relocate, EEO choices (including "decline to answer"), salary expectations if you want them. Every `ask_user` answer can be saved for reuse. The application email comes from your profile and is recorded as `applied_with_email`.

### Safety rules for the Applier
- **Navigation pinned:** the browser tool refuses any URL outside the job's ATS domain, plus its known asset and SSO domains.
- **Credentials never reach the model:** logins are typed by the tool, from the macOS Keychain on the runner, if you choose to store them.
- **Submit is gated:** dry run is on by default for every ATS. You can switch an ATS to automatic submit after it has a clean track record, and there's a daily cap on submissions.
- **Always pause and ask** on essays and cover letters, "are you a human/AI" or attestation questions, CAPTCHAs, anything not in your answers bank, any page asking for a password you haven't stored, and any companies routed to "apply myself".
- **No CAPTCHA solving and no bot-detection evasion.** It's your browser, your answers and your pace.
- **Every action is logged** in `run_events` with screenshots before submit and after confirmation.

## Model layer

```python
class Model(Protocol):
    def complete(self, messages, *, schema=None, tools=None, budget) -> Completion: ...


# Completion: text | parsed (validated against schema) | tool_calls, usage, cost_usd, model_id
```

- **v1 adapter:** OpenAI-compatible client with base URL `https://openrouter.ai/api/v1`, using `OPENROUTER_API_KEY` from the environment.
- **Structured output:** request a JSON schema, then **always validate** in our code. On a bad response, retry once with the error message, then fail the step cleanly. Models differ a lot here, and it shows up in the eval.
- **Model allowlist** in settings (e.g. `anthropic/claude-haiku-…`, `meta-llama/…`, `qwen/…`), with a default per role. The UI picks from the allowlist, so a typo can't select an unexpectedly expensive model.
- **OpenRouter data policy:** in OpenRouter's privacy settings, disallow providers that train on or retain prompts. Your profile and answers go through it.
- **Eval:** about 30 postings you've already labeled. `appa eval --model X` reports agreement per rule and cost, which is how you'll compare models.

## Configuration: where everything lives

| What | Where (v1) | Edited by |
|---|---|---|
| Hard rules (sponsorship needs, max years, title filters, rule toggles) | `policy` row in Postgres, seeded from `config/policy.yaml` | UI (`policy` command), validated |
| Judgment (`profile.md`) | `profile` row in Postgres, seeded from `config/profile.md` | UI editor |
| Your facts for applications | `answers` table | UI, and `ask_user` answers you choose to save |
| Résumé file | on the runner machine (path in runner config); server keeps hash + parsed text | you |
| ATS logins (optional) | macOS Keychain on the runner machine | you |
| Model allowlist, role defaults, budgets | `config/models.yaml` | file (operator setting) |
| Secrets (`OPENROUTER_API_KEY`, DB password, session secret) | environment / `.env` on the server (never in git) | you |
| Company boards to poll | `boards` table | UI / CLI |

## Data model additions

```
runs           id, role, model, status, input jsonb, budget jsonb, cost_usd, steps,
               started_at, finished_at, error, created_by (user | schedule | api key)
run_events     id, run_id, ts, kind (log | model_call | tool_call | question | result),
               payload jsonb            -- what the terminal UI streams
jobs           id, kind, payload, run_id, status, attempts, lease_until, available_at
schedules      id, role, model, cron, input, enabled, next_run_at
questions      id, run_id, prompt, answer, answered_at        -- ask_user
answers        id, key (normalized question), value, source, updated_at
apply_attempts id, application_id, job_key, ats, status (filling | awaiting_approval |
               submitted | confirmed | needs_you | failed), fields jsonb, screenshots,
               approved_by, submitted_at
uploads        id, sha256, mime, bytes, path, created_at      -- screenshots
profile/policy versioned rows (keep history of what rules a decision used)
users          (v1: exactly one) id, username, password_hash (argon2id), totp_secret
```

Existing tables (`applications`, `seen_jobs`, `api_keys`) carry over. `seen_jobs` gains `run_id` and the `policy_version` used.

## API surface (v1)

| Route | Auth |
|---|---|
| `POST /api/auth/login`, `POST /api/auth/logout` | public, rate-limited (5/min/IP) |
| `GET/PUT /api/profile`, `GET/PUT /api/policy` | session |
| `POST /api/runs` `{role, model, input, budget}` · `GET /api/runs` · `POST /api/runs/{id}/cancel` | session or key with `runs:write` |
| `GET /api/runs/{id}/stream` (SSE) | session |
| `POST /api/uploads` (screenshots) | session, rate-limited |
| `GET/POST /api/questions`, `POST /api/questions/{id}/answer` | session |
| `GET/POST/DELETE /api/schedules` | session |
| `/api/v1/applications/*` (exists today) | key scopes, as now |
| `GET /api/runner/next`, `POST /api/runner/jobs/{id}/events`, `POST /api/runner/jobs/{id}/result` | runner-only key (`runner` scope) |
| `GET /api/apply/pending` · `POST /api/apply/{id}/approve` · `/skip` | session |
| `GET/PUT /api/answers` | session |

## Web UI (terminal-style)

One screen: a command line, a scrolling output pane fed by SSE, and a side panel for the review queue and questions. Commands map 1:1 to API calls:

```
scout [--model m] [--boards all|<slug>…]     add <url>…        upload (drag & drop)
apply <id> | approve <id> | skip <id>          answers [edit]     runner status
review                                         answer <id> …      apply-myself list
schedule scout "0 */4 * * *" --model m         runs | cancel <id> | cost [--since 7d]
profile edit                                   policy show|set …
```

React + Vite, using framework escaping only (never raw HTML from data). All calls go to `/api` on the same origin.

## Security design

**Edge**
- Caddy: automatic TLS, HSTS, security headers, request body limit (10 MB for uploads, 64 KB elsewhere).
- Only 443 is public; the VPS firewall allows SSH (keys only) and 443.

**Humans**
- One user in v1, with argon2id password hashing and optional TOTP.
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, plus a CSRF token on writes.
- Login rate limit and lockout backoff.

**Agents and machines**
- Existing hashed, scoped API keys, with new scopes `runs:write` and `runner`.

**Runs**
- Budgets on every run, global daily spend cap and a kill switch (`appa stop-all`).
- Every tool call is in `run_events` (the audit trail).

**Tools are capabilities**
- Each role can only call its own tools.
- `fetch_posting` goes through the existing outbound-URL guard.
- Applier: navigation is pinned to the job's ATS domain, credentials are inserted by the tool (the model never sees them), `submit` is gated and dry-run first, and a CAPTCHA or "are you human/AI" prompt pauses the run and asks you.

**Uploads**
- MIME checked from file content (not the client's claim): PNG, JPEG and WebP only.
- 10 MB cap at both Caddy and the app.
- Re-encoded with Pillow, which strips EXIF and location data and neutralizes polyglot files.
- Stored by hash outside the web root.

**Database**
- Not published.
- The app's database role has no DDL rights; migrations run as a separate role.
- 10 s statement timeout, bounded pool.
- Nightly `pg_dump`, encrypted, to object storage.

**Posting text is hostile.** It stays as data only, schema-bound, with quote verification and keyword cross-checks, as today.

## Deployment

```
deploy/
  docker-compose.yml   caddy, api, worker, scheduler, postgres (+ backup job)
  Caddyfile            TLS, headers, limits, / → static, /api → api
  .env.example         every secret listed, no values
```

- **Server:** any $5–10/month VPS with 2 vCPU and 2–4 GB RAM. Hetzner is the cheapest; your credits may cover others.
- **Hardening checklist:** non-root deploy user, SSH keys only, firewall (22 and 443), automatic security updates, fail2ban.
- **Deploy flow:** `git pull && docker compose up -d --build`. Migrations run in a one-off container before the api starts.
- **Local dev:** the same compose file, plus a dev override that publishes the API only on `127.0.0.1`.

## Cost model (to verify by measurement)

| Item | Estimate |
|---|---|
| VPS | $5–10 / month |
| Screening per posting (cheap model via OpenRouter) | ~$0.0005–0.003; free filters run first, so most postings never reach a model |
| Screenshot read | ~$0.002–0.01 each |
| Applier per application | ~$0 for fully deterministic fills (Greenhouse/Lever/Ashby standard fields); ~$0.01–0.10 when the model handles unusual fields; measured in M6 |
| Backups / object storage | ~$0–1 / month |

The daily spend cap enforces whatever ceiling you set.

## Repo layout

```
appa/              python package (today's code + new modules)
  api/             routes split by area
  agents/          roles: scout.py, screener.py, concierge.py (+ prompts/*.md)
  llm/             model protocol, openrouter adapter, schema validation, cost
  runtime/         runs, budgets, events, queue (claim/lease/retry), scheduler
  tools/           tool registry + implementations (wrap sources/, db)
  core/            rules, jobid, normalize, models (moved, unchanged)
  sources/         unchanged
migrations/        Alembic
web/               React + Vite terminal UI
deploy/            compose, Caddyfile, .env.example
config/            policy.yaml, profile.example.md, models.yaml
docs/
tests/             + Postgres-backed integration tests
```

## Milestones to the first push

| # | Milestone | Done when |
|---|---|---|
| M1 ✅ | Postgres + Alembic; move existing code into the layout above; all 80 tests green on Postgres | Behavior unchanged |
| M2 ✅ | Model layer (OpenRouter) + eval command | `appa eval --model X` prints accuracy and cost on your labeled set |
| M3 ✅ | Runtime: runs, events, queue, worker, budgets, scheduler; Scout, Screener, Concierge as roles; `profile.md` routing | `appa run scout --model X` runs through the worker, streams events and respects budgets |
| M4 ✅ | API v1 routes + session login + uploads | Integration tests for auth, CSRF, upload validation, run lifecycle |
| M5 ✅ | Terminal web UI | Start, watch, cancel, schedule and review from the browser |
| M6 ✅ (built; your 10 approvals pending) | Runner + Applier: Greenhouse/Lever/Ashby fillers, answers bank, `ask_user` pause/resume, dry-run approval, gated submit, confirmation detection | Dry runs on real postings fill every standard field correctly; you approve 10 real applications end to end |
| M7 | Deploy bundle + hardening checklist + backups | Running on the VPS with only 443 open, runner connected from your Mac; you've reviewed everything |
| **Push v1** | You review the code and docs, then we commit and push | |
| v2 | Email intake; Workday/iCIMS support; hosted browser option for when your Mac is off | |

## Open questions

1. **VPS provider:** cheapest (Hetzner), or where your YC credits are? Which credits do you have?
2. **Domain:** you'll need one for TLS (any cheap domain works).
3. **Login:** password + TOTP for v1, or sign-in with GitHub?
4. **Contribution terms:** CLA or DCO, before the first outside PR (see ROADMAP).
5. **Eval labels:** pick about 30 postings you've already judged (pass, reject and why) to build the first eval.
6. **Answers bank seed:** your standard answers (contact, links, work authorization wording, relocation, EEO preferences, salary) and which résumé file to use.
7. **ATS logins:** store any in the Keychain for the runner, or always sign in yourself when asked?
