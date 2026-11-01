# appa

appa finds new-grad jobs everywhere, screens each one against your rules, and surfaces the ones
worth your time. You review them and you apply. **appa never applies for you.**

```
find  ──►  screen  ──►  surface  ──►  you review and apply
47 job lists         sponsorship, citizenship,     Matches in the web app,
1,500+ company       clearance, years, level,      with why each one matched
boards (42 ATSs)     your own preferences          and what to double-check
```

- **Find.** Every few hours appa reads 47 job lists (SimplifyJobs and a dozen other new-grad lists,
  Hacker News "Who is hiring", remote boards, Google and Microsoft careers, plus job APIs once you add
  their keys). Every link it sees that points at a company's applicant-tracking system adds that
  company's whole board to a rotation (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable,
  iCIMS, Oracle and 34 more). Free filters drop what clearly doesn't fit before any model call.
- **Screen.** A model reads each remaining posting and extracts facts (sponsorship stance,
  citizenship or clearance, years required, seniority), each with a verbatim quote. Plain code makes
  the decision (`appa/core/rules.py`). Nothing is rejected without quoted evidence; anything unclear
  goes to "Check first". With no model key, a keyword screen runs instead and never rejects.
- **Surface.** Good matches land on the **Matches** page with the reasons and the quotes. Open the
  posting, apply yourself, and mark it applied. If you like, appa can **prefill** the form in your own
  browser and leave it open for you to check and submit.

Posting text is treated as untrusted: hidden text aimed at AI tools is pulled out, labeled, and sends
the job to review. The database (PostgreSQL) is the source of truth for everything you've applied to.

**Docs:** [Job sources](docs/JOB-SOURCES.md) · [Tour of the code](docs/TOUR.md) · [Architecture](docs/ARCHITECTURE.md) · [Security](docs/SECURITY.md) · [Roadmap](docs/ROADMAP.md)

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
brew install postgresql@17          # macOS; any PostgreSQL 15+ works
scripts/dev-db.sh init              # local database in data/pg (127.0.0.1 only), roles, .env with random passwords
.venv/bin/appa migrate              # create the schema
.venv/bin/appa init                 # creates config/profile.yaml: edit your sponsorship situation and filters
.venv/bin/appa user create <you>    # your web login (then `appa user totp <you>` for two-factor codes)
npm --prefix web install
```

Add your keys to `.env` (gitignored, never committed): at least `OPENROUTER_API_KEY` for screening.
See [Keys](#keys) for the free job-source keys.

Run it (four small processes; deployed, they run as services):

```bash
appa serve                          # the API, on 127.0.0.1:8787
appa worker                         # runs searches and screening
appa scheduler                      # starts searches on schedule
npm --prefix web run dev            # the web app: http://127.0.0.1:5173
```

Then turn on the schedule once, from **Sources → Search every 6 hours** in the web app, or:

```bash
appa schedule add scout "0 */6 * * *" --sweep
```

Optional: import your existing tracker (`appa backfill ~/Downloads/tracker.csv`, safe to re-run).

## Using the web app

| Page | What it's for |
|---|---|
| **Matches** | Your review queue. *Good matches* passed every check; *Check first* has something worth a look (sponsorship wording, years, an odd page); *Apply directly* are companies you prefer to handle yourself. Click one for the checks and quotes. Actions: **Open posting**, **I applied**, **Prefill in my browser**, **Not for me** (with undo). Keys: `j`/`k` move, `enter` details, `o` open, `a` applied, `x` not for me. Questions only you can answer appear at the top under **Needs you**. |
| **Applied** | Everything you've applied to and where it stands (Applied, Interviewing, Offer, Rejected…). Change a status in place. |
| **Discover** | Everything the sources found: waiting to be screened, screened, and *set aside* by a free filter with the reason. Add anything to your matches, or restore what a filter got wrong. |
| **Sources** | Which job lists are connected, which need a key, the company boards in rotation, the schedule, and **Search now**. |
| **Activity** | Every search and prefill, with its log and cost. |
| **Settings** | Filters (titles, years, age, US only, companies to skip or handle yourself), your preferences in plain words, and the saved answers used to prefill forms. |

A typical day: open Matches, skim the good ones, open the postings you like, apply, press **I applied**.

## Prefill (optional)

appa can fill an application form in a dedicated Chrome window on your Mac, then hand it to you. It
never presses submit: there is no code that can. You check the form, submit it yourself, and mark it
applied.

```bash
appa answers import config/answers.yaml           # your facts (copy config/answers.example.yaml)
appa keys create my-mac --scopes runner           # once; put it in APPA_RUNNER_KEY
APPA_RUNNER_KEY=appa_... appa runner --resume ~/Documents/resume.pdf
```

Then press **Prefill in my browser** on a match (or `appa apply <id>`). The runner reads the form;
anything your saved answers don't cover (essays, unusual questions) becomes a **Needs you** card, and
your answer is reused on every later form. When it's filled, the tab stays open, appa's browser guard
lifts for that tab, and a card says it's ready. Résumés go only in résumé fields; optional cover
letters stay empty.

Sites that need an account (Workday, iCIMS, Taleo, SuccessFactors, Oracle, Handshake), LinkedIn,
login walls, multi-page wizards and bot-protection pages are handed back to you with the reason. appa
never signs in, creates accounts, solves CAPTCHAs or gets around bot protection.

## Keys

Put these in `.env`. Only the first matters for screening; the rest add job sources.

| Key | Where | Adds |
|---|---|---|
| `OPENROUTER_API_KEY` | openrouter.ai/keys | Model screening (about $0.0006 a posting; capped at $5/day in `config/models.yaml`) |
| `THEIRSTACK_API_KEY` | theirstack.com | Jobs from 200k+ company sites (200 free credits/month) |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | developer.adzuna.com | A broad US job aggregator |
| `THEMUSE_API_KEY` | themuse.com/developers | The Muse (low precision) |
| `SERPAPI_API_KEY`, `RAPIDAPI_KEY` | serpapi.com, RapidAPI (JSearch) | Google Jobs results (off by default; add to `feeds.lists`) |
| `JOOBLE_API_KEY`, `FINDWORK_API_KEY`, `USAJOBS_API_KEY` + `USAJOBS_EMAIL` | each site's developer page | More aggregators (off by default) |

`appa feeds` (or the Sources page) shows which are ready.

## Command line

Everything in the web app is also a command:

```bash
appa sweep                          # search every source now, then screen the newest
appa feeds                          # sources and what each needs
appa listings [--status filtered]   # what was found; `listings track ID`, `listings restore ID`, `listings refilter`
appa boards                         # company boards in rotation; `boards add workday:acme:wd5:External`
appa add <url>                      # screen one posting you found yourself
appa list --verdict review          # recent screening decisions
appa applied <id>                   # you applied: mark it
appa apply <id>                     # prefill it in your browser (you submit)
appa takeover <id>                  # stop a prefill; you'll apply from scratch
appa runs                           # recent runs; `appa runs <id>` follows one
appa stop-all / appa resume         # pause everything / undo
appa insights                       # what appa missed most often (the next fix worth writing)
appa export out.csv                 # every application, for Google Sheets
```

Every run has a budget (`--max-usd`, `--max-steps`), and `limits.daily_usd_cap` caps all runs combined.

## Screening rules

| Verdict | Meaning | Where it shows |
|---|---|---|
| `candidate` | Passes every rule | Matches → Good matches |
| `manual` | A company you handle yourself (your list, or your preferences in plain words) | Matches → Apply directly |
| `review` | Unclear sponsorship wording, a staffing agency, AI-aimed hidden text, or the posting couldn't be read | Matches → Check first |
| `reject` | Fails a rule, with the quote that shows it | Discover → Screened |
| `duplicate` | Its job ID is already tracked | Discover |

**A posting's identity is its job ID** (in the link, the page's structured data, or the source's own
ID; see `appa/core/jobid.py`). Only a matching job ID makes a duplicate. Applications with no link or
no job ID never block anything: a duplicate application beats a missed one.

"No sponsorship" isn't treated as "no OPT": postings that refuse sponsorship without saying "now or
in the future" go to Check first, not reject.

## Models and evals

Models come from [OpenRouter](https://openrouter.ai) through one adapter (`appa/llm/`).
`config/models.yaml` holds the allowlist, the default per job, and routing preferences (providers
that store or train on prompts are excluded). Every call reports its real cost.

```bash
appa eval                                         # default screener on all labeled cases
appa eval --model anthropic/claude-haiku-5.5 --model deepseek/deepseek-v4.1-flash   # compare
```

## API

Everything is under `/api/v1`. You sign in with a session (HttpOnly, Secure, SameSite=Strict cookie
plus a CSRF header on writes); agents use scoped API keys (`appa keys create <name> --scopes …`),
which can't reach human-only routes.

| Route | Who | Use |
|---|---|---|
| `GET /health` | public | Liveness |
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | public (rate-limited) · you | Sessions |
| `GET /system/overview` · `GET /system` · `POST /system/stop-all` · `/resume` | you | Home screen numbers, status, kill switch |
| `GET /applications?status=&status=&q=` · `GET/PATCH /applications/{id}` | read · write | Your applications (detail includes screening and prefill) |
| `POST /applications` · `GET /applications/check` | write · read | Agents record applications and check before applying |
| `POST /applications/{id}/applied` · `/apply` · `/takeover` | you | Mark applied · prefill in your browser · stop a prefill |
| `GET /questions` · `POST /questions/{id}/answer` | you | Needs-you cards: `answer`, `applied`, `skip`, `choose`, `approve`, `dismiss` |
| `GET /listings` · `POST /listings/{id}/track` · `/skip` · `/restore` · `GET /feeds` | read · write | What the sources found, and the sources |
| `POST /runs` · `GET /runs` · `GET /runs/{id}/events` · `/stream` · `/cancel` | `runs:*` | Start and watch searches |
| `GET/POST /schedules` · `PATCH/DELETE /schedules/{id}` | you | Recurring searches (cron, UTC) |
| `GET/PUT /profile` · `GET/PUT /policy` · `GET/PUT/DELETE /answers` | you | Preferences, filters (versioned), saved answers |

The server only binds to localhost. Deployed, Caddy terminates TLS, serves the built `web/dist` and
proxies `/api`, so the browser only ever talks to one origin and 443 is the only public port.

## Not built yet

Email ingest (job-alert emails into the inbox), a Telegram bot, live Google Sheets sync, and the
deployment scripts.

## License

[GNU AGPL-3.0-or-later](LICENSE). You can use, modify and self-host appa freely. If you run a
modified version as a network service for others, you must offer those users your source code under
the same license.
