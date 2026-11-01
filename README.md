# appa

Personal job-intake pipeline and application database. Give it job links,
screenshots of job feeds, or a list of companies. It finds the real posting,
filters out roles you can't or shouldn't apply to, and records the rest. It
checks everything against what you've already applied to. Your agents can
record applications through an authenticated API.

```
source → fetch posting → dedupe vs applications DB → title pre-filter → model extracts facts → rules decide → DB
```

- **The model only extracts facts** (sponsorship stance, citizenship/clearance, years required, seniority), each with a verbatim quote. Any model on OpenRouter can do it, chosen per run. **Plain code makes the decision** (`appa/core/rules.py`).
- **Posting text is treated as untrusted.** Hidden text on a page (`display:none`, zero-size fonts) is pulled out and labeled. Anything aimed at AI tools is reported in `suspicious_instructions`, and the job is sent to review.
- **The database (PostgreSQL) is the source of truth.** Your old sheet is imported once with `appa backfill`, and `appa export` writes a CSV back out for Google Sheets. Rejected postings are kept separately (`appa list --verdict reject`), not as applications.

**Docs:** [Architecture](docs/ARCHITECTURE.md) · [Security](docs/SECURITY.md) · [Roadmap](docs/ROADMAP.md)

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
brew install postgresql@17          # macOS; any PostgreSQL 15+ works
scripts/dev-db.sh init              # local cluster in data/pg (127.0.0.1 only), roles, .env with random passwords
.venv/bin/appa migrate              # create the schema (runs as the owner role)
.venv/bin/appa init                 # creates config/profile.yaml
.venv/bin/pytest                    # database tests use the separate appa_test database
# then put OPENROUTER_API_KEY=... in .env (gitignored); never commit it
```

Edit `config/profile.yaml` with your sponsorship situation, experience cutoff, and apply-myself companies. Then import your existing tracker:

```bash
appa backfill ~/Downloads/tracker.csv      # safe to re-run; already-imported rows are skipped
```

## Use

Everything appa does is a **run** of an agent role (Scout, Screener, Concierge), recorded step by
step with its cost and stopped by its budget.

```bash
appa add <url> [<url> ...]          # Greenhouse / Lever / Ashby links, company careers pages (not LinkedIn)
appa screenshot feed.png            # LinkedIn/Simplify screenshot → finds each real posting
appa poll                           # check every board under `companies:` for new postings
appa add <url> --model qwen/qwen3.6-35b-a3b --max-usd 0.05   # any allowlisted model, any budget
appa list --verdict review          # see what needs your eyes
appa questions                      # what runs are asking you
appa export out.csv                 # all applications, for Google Sheets
```

`add`, `screenshot` and `poll` run in your terminal. Add `--queue` to hand them to a worker.

## Every posting, automatically

`appa sweep` is the one command to schedule. Each sweep does three things, sized by `feeds:` in your
policy:

1. **Reads every job source** into an inbox of listings. Sources include the SimplifyJobs new-grad
   list, Hacker News "Who is hiring", The Muse, remote boards, the Google and Microsoft career
   feeds, and Adzuna/USAJOBS/Jooble/Findwork once you add their free keys. No model is called.
   Free filters drop what clearly doesn't fit: your title rules, the source's own citizenship or
   internship labels, postings older than `max_age_days`, and locations clearly outside the US.
2. **Discovers company boards.** Every link that points at a company's ATS (Greenhouse, Lever,
   Ashby, Workday, SmartRecruiters, Workable, Recruitee, BambooHR, Breezy, Personio, Teamtailor,
   Rippling…) adds that company's whole board to a rotation. Each sweep polls the next
   `boards_per_run` boards, which finds roles the lists never carry.
3. **Screens the newest listings.** With a model key it uses the full screener. Without one it
   uses a keyword screen: anything mentioning sponsorship limits, citizenship, clearance, too many
   years or hidden text goes to review, with the passage quoted. Nothing is rejected without a
   model's verbatim evidence.

```bash
appa sweep                          # everything, now (add --queue for a worker)
appa sweep --no-screen              # just collect into the inbox
appa schedule add scout "0 */6 * * *" --input '{"sweep": true}'   # every 6 hours
appa feeds                          # sources, what each needs, inbox and board counts
appa listings                       # new listings; --status filtered shows what was dropped and why
appa listings track 812             # you want it: To Apply (no screening)
appa listings restore 640           # a filter was wrong: screen it after all
appa boards                         # boards in rotation (most useful first); boards add workday:acme:wd5:External
```

Candidates land in your applications as To Apply. With `apply.auto_prepare: true` in your policy,
each is filled in your browser right away, and every one still waits for your approval before submit.

**Taking over.** `appa takeover <application id>` (or `takeover ID` in the UI) makes any job yours. It
stops appa's attempt, closes its questions, marks the job Apply Myself and gives you the link. The one
exception is a submit you already approved, which runs to its result.

## Agents, workers and schedules

```bash
appa worker                         # executes queued runs (run several for parallelism)
appa scheduler                      # turns schedules into queued runs
appa run scout --board config       # queue any role; streams its events live
appa run concierge --inline         # or run it right here
appa schedule add scout "0 */4 * * *" --board config --max-usd 0.25    # cron, UTC
appa runs                           # recent runs; `appa runs <id>` replays or follows one
appa cancel <id>                    # stop one run
appa stop-all                       # kill switch: pause workers, cancel everything; `appa resume` to undo
```

| Role | Does |
|---|---|
| `scout` | Finds postings (links, screenshots, company boards) and screens each one |
| `screener` | Screens specific links: hard rules in code, then **who applies**, decided from `config/profile.md` |
| `concierge` | Turns review items and apply-myself jobs into questions for you (no model calls) |

**Who applies** is judgment, so it's written in plain language in `config/profile.md` (copy
`config/profile.example.md`): e.g. "big tech and companies big in software, like Datadog and
HubSpot, I apply to myself". That decision sees only company, title and location, never posting
text. The `manual_companies` / `always_auto` / `never_apply` lists in `profile.yaml` override it.

Budgets: every run has `max_usd`, `max_steps` and `max_model_calls` (role defaults, override with
`--max-usd` / `--max-steps`), and `limits.daily_usd_cap` in `config/models.yaml` caps all runs combined.

## Models and evals

Models come from [OpenRouter](https://openrouter.ai) through one adapter (`appa/llm/`).
`config/models.yaml` holds the allowlist (only listed models can be picked), the default
per job, and routing preferences: providers that store or train on prompts are excluded.
Every call reports its real cost.

```bash
appa eval                                         # default screener on all labeled cases
appa eval --model anthropic/claude-haiku-5.5 --model deepseek/deepseek-v4.1-flash   # compare
appa eval snapshot <url>                          # save a real posting to evals/local/ to label
```

The eval scores each model's extraction and the resulting verdict against labeled postings:
16 synthetic cases in `evals/screening/` plus your own in `evals/local/` (gitignored). It reports
**false rejects** (jobs you'd lose), **missed rejects** (no-go jobs it would auto-apply to),
per-field accuracy, errors and cost. `--max-cost` (default $1) stops a run that gets expensive.

## Applying in your browser

appa fills applications **on any site that doesn't need a login**, in a dedicated Chrome profile on
your Mac, and submits only what you approve. Greenhouse, Lever and Ashby have tuned support; anywhere
else, the runner finds the form itself (following an "Apply" button or an embedded form).

```bash
appa answers import config/answers.yaml           # your facts (copy config/answers.example.yaml)
appa keys create my-mac --scopes runner           # once; put it in APPA_RUNNER_KEY
APPA_RUNNER_KEY=appa_... appa runner --resume ~/Documents/resume.pdf
appa apply 412                                    # or: appa apply --all-to-apply, or `apply 412` in the UI
```

1. **Prepare:** the runner opens the form and reports its questions. The server plans each field from your answers bank. Anything it can't answer becomes a question for you (essays, "are you human/AI" checks, unusual questions), and your answer is saved for every future form. Then the runner fills the form and sends a screenshot.
2. **Approve:** the UI shows the filled form and exactly what will be submitted, and where each value came from. Nothing is submitted until you click **submit application** (or `appa approve <attempt>`).
3. **Submit:** the runner fills the form again, checks every field matches what you approved, submits, and looks for the confirmation page. A CAPTCHA pauses it for you to solve in the window. No confirmation page means it asks you instead of marking the application Applied.

Limits: `apply.daily_submit_cap` (default 10 per 24h) in your policy. Set `apply.auto_prepare: true` to
fill "To Apply" jobs automatically as they're found (still approval-gated).

**Handed back to you, with the reason:** sites that need an account (Workday, iCIMS, Taleo,
SuccessFactors, Oracle, Handshake), LinkedIn and Indeed, login walls, multi-page application wizards,
"Apply" buttons leading to another site, and bot-protection pages. appa never signs in, creates
accounts, solves CAPTCHAs or gets around bot protection.

## What to improve next

Every miss and failure is recorded: links whose job ID no pattern read, form questions no
standard answer covers, answers only the model could map, fields that didn't fill, sites handed
back, submits with no confirmation page, and extraction errors. Only labels, links and errors are
kept, never your answers.

```bash
appa insights                       # most common misses, grouped by kind, site and subject
appa insights --kind label_unmapped # one kind, with the latest example
appa insights --json                # for an agent to work through
appa insights --backfill            # once: seed from postings and attempts recorded so far
```

The top rows are the next regex, label pattern or site fix worth writing. Where agents can get
postings at all is in [docs/JOB-SOURCES.md](docs/JOB-SOURCES.md).

## Web UI

A terminal in the browser (`web/`, React + TypeScript): type commands, watch runs stream live,
answer your question queue with one click, drag screenshots in to scout them.

```bash
appa user create <you>              # once
appa serve & appa worker &          # API on 127.0.0.1:8787, plus a worker
npm --prefix web install
npm --prefix web run dev            # http://127.0.0.1:5173 (proxies /api to the API)
```

Type `help` in the UI for every command (`poll`, `add URL`, `upload`, `follow ID`, `schedule add …`,
`review`, `answer ID approve`, `profile edit`, `stop-all`, …). Deployed, Caddy serves the built
`web/dist` and proxies `/api` the same way, so the browser only ever talks to one origin.

## API

Everything is under `/api/v1`. Two ways in:

- **You, in the browser:** `appa user create <name>` (then optionally `appa user totp <name>` for 2FA)
  and log in at `POST /api/v1/auth/login`. The session cookie is HttpOnly, Secure and SameSite=Strict, and
  every write must echo the session's CSRF token in an `X-CSRF-Token` header.
- **Agents and scripts:** `appa keys create <name> --scopes runs:read,runs:write` and send
  `Authorization: Bearer <key>`. Keys are scoped, stored hashed and rate-limited, and can't reach
  human-only routes (profile, policy, uploads, questions, schedules).

```bash
appa serve                          # http://127.0.0.1:8787, localhost only
```

| Route | Who | Use |
|---|---|---|
| `GET /health` | public | Liveness only |
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | public (5/min/IP) · you | Session login; `me` returns the CSRF token |
| `POST /runs` `{role, model?, input, budget?}` | you or `runs:write` | Start an agent run (queued for a worker) |
| `GET /runs` · `GET /runs/{id}` · `GET /runs/{id}/events?after=` | you or `runs:read` | Run status and its recorded events |
| `GET /runs/{id}/stream` | you or `runs:read` | Live Server-Sent Events; resumes with `Last-Event-ID` |
| `POST /runs/{id}/cancel` | you or `runs:write` | Stop a run |
| `POST /uploads` (multipart `file`) | you | Screenshot → `{id}`; pass as `input.upload_ids` to a Scout run |
| `GET /questions` · `POST /questions/{id}/answer` | you | Your queue: `approve`, `apply_myself`, `skip`, `dismiss`, `choose` |
| `GET/POST /schedules` · `PATCH/DELETE /schedules/{id}` | you | Recurring runs (cron, UTC) |
| `GET/PUT /profile` · `GET/PUT /policy` | you | `profile.md` and hard rules; every save is a new version |
| `GET /roles` · `GET /models` | you or `runs:read` | What the UI needs for its menus |
| `GET /applications/check?company=&role=&link=` | you or `applications:read` | Before applying: `already_applied: true` (same job ID) is the only "don't apply" answer |
| `POST /applications` | you or `applications:write` | After applying. 409 with the existing record only if the link's job ID is already tracked |
| `PATCH /applications/{id}` · `GET /applications?status=&company=&q=` | write · read | Update; list or search |

```bash
curl -s -X POST localhost:8787/api/v1/applications -H "Authorization: Bearer $APPA_KEY" \
  -H 'content-type: application/json' \
  -d '{"company":"Figma","role":"Software Engineer, New Grad","link":"https://job-boards.greenhouse.io/figma/jobs/123"}'
```

The server refuses to bind anything but localhost. Deployed, Caddy terminates TLS in front of it
and is the only public port.

**A posting's identity is the job ID in its link** (Greenhouse/Lever/Ashby IDs, `gh_jid`/`ashby_jid`, Microsoft `pid`, Workday `_JR-123`, Amazon/iCIMS/LinkedIn IDs, and so on; see `appa/core/jobid.py`). It's stored as `job_key` and unique in the database. Only a matching job ID makes a duplicate. The same title at the same company with a different ID is a separate posting. **Applications with no link or no job ID never block anything:** a duplicate application beats a missed one.

## Verdicts

| Verdict | Meaning | Status recorded |
|---|---|---|
| `candidate` | Passes every rule | To Apply |
| `manual` | On your apply-myself list | Apply Myself |
| `review` | Unclear sponsorship wording, staffing agency, AI-aimed text, a different posting at a company you've applied to (only if `same_company: flag`; the default `allow` just adds a note), or the posting couldn't be read | Needs Review |
| `reject` | Fails a rule | (seen-jobs log only) |
| `duplicate` | This job ID is already tracked | (seen-jobs log only) |

## Not built yet

Email ingest (job-alert emails into the inbox), a Telegram bot, live Google Sheets sync, and deployment.

## License

[GNU AGPL-3.0-or-later](LICENSE). You can use, modify and self-host appa freely. If you
run a modified version as a network service for others, you must offer those users
your source code under the same license.
