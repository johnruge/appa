# Where agents can get job postings

Researched and probed on 2026-10-30. "Probed" means a live request returned 200 with postings.
Terms change, so re-check a provider's terms before adding it as a source.

## Why appa has a fast path for Greenhouse, Lever and Ashby

On the SimplifyJobs new-grad list, 844 of the 3,106 active listings (27%) are on those three.
They give appa three things no other site does:

- **Structured postings for free.** A public JSON API returns the full description, location
  and ID. No scraping and no model call is needed to read the page (`sources/ats.py`).
- **Canonical apply URLs.** appa opens the form directly (`apply/service.apply_url`).
- **Known form markup.** Filling on these is tuned and tested (`runner/dom.py`).

Everything else uses the generic path. The page is fetched, structured data and a verified AI
read give the ID, and the runner finds the form. `_PATTERNS` is the shortcut, not the limit.
`appa insights --kind id_not_in_link` shows which other sites are worth a shortcut next.

## Company job boards (one company per call, free, no key)

| ATS | Endpoint | Probed |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | yes (in appa) |
| Lever | `GET api.lever.co/v0/postings/{slug}?mode=json` | yes (in appa) |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{slug}` | yes (in appa) |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings` | yes. An unknown company returns 200 with 0 results, so empty doesn't mean "no jobs" |
| Recruitee | `GET {slug}.recruitee.com/api/offers/` | yes |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{slug}`, or `POST …/api/v3/accounts/{slug}/jobs` | yes. 404 for unknown accounts; rate-limits by IP |
| Personio, Teamtailor, BambooHR | per-customer feeds that must be enabled | not probed; sources disagree |

Workable (118 listings) and SmartRecruiters (91) are the next biggest on the new-grad list, so
they're the next board adapters worth writing.

## Aggregated lists and feeds

| Source | What | Key | Notes |
|---|---|---|---|
| **SimplifyJobs New-Grad-Positions** | Curated new-grad SWE/quant/PM roles, updated daily | none | `raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json`. 19k rows (3.1k active) with company, title, locations, url, date_posted and a sponsorship field. **Best fit for you.** Check the repo's license before redistributing |
| The Muse | General jobs, filterable by `level=Entry Level` | optional | Requires a link back; delete copies when you stop using it |
| USAJOBS | US federal jobs | free key | Mostly citizenship-only, so a poor fit for you. Use is limited to the registered user |
| Adzuna | Broad aggregator | free key | 250 calls/day. Restricts reuse of its data |
| Remotive, Arbeitnow | Remote / Europe-heavy feeds | none | Small for US new-grad roles |
| SerpApi (Google Jobs) | Google's job results | paid | 250 searches/month free, then from $25/month |
| JSearch (RapidAPI) | Google Jobs wrapper | paid | 200 requests/month free |
| TheirStack | Multi-source aggregator | paid | About $49–59/month entry |

Not usable: Indeed's Publisher API is retired, and LinkedIn forbids scraping (appa uses your alert
emails and screenshots instead).

**Recommended order for appa:** a SimplifyJobs scout (free, daily, new-grad only, links mostly
land on the fast-path ATSs). Then Workable and SmartRecruiters board adapters. A paid search API
only if those leave gaps.

Sources: [JobsPipe: free job APIs](https://jobspipe.dev/blog/free-job-api),
[JobsPipe: public vs gated ATS APIs](https://jobspipe.dev/blog/public-vs-gated-ats-apis),
[adderleydata/ats-job-board-api](https://github.com/adderleydata/ats-job-board-api),
[SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions),
[Adzuna API](https://developer.adzuna.com/), [SerpApi Google Jobs](https://serpapi.com/google-jobs-api),
[JobsPipe: JSearch](https://jobspipe.dev/blog/jsearch-api-direct),
[JobsPipe: best jobs APIs 2026](https://jobspipe.dev/blog/best-jobs-api-2026).
JobsPipe sells a competing API, so its comparisons were checked against the live probes above.

## Every source appa reads (generated from the registry)

47 list sources and 42 ATS board types. `appa feeds` shows which are ready on your machine.

### List sources (many companies per call)

| Source | Needs | In `all` | What |
|---|---|---|---|
| `adzuna` | ADZUNA_APP_KEY, ADZUNA_APP_ID | yes | Adzuna search API: aggregated US postings from many boards and employer sites, newest first |
| `ambicuity_newgradjobs` | nothing | yes | ambicuity New-Grad-Jobs (jobs.riteshrana.engineer): new-grad/entry roles scraped from ~900 employer ATS boards every 30 min; tech categories only |
| `applyguy_newgrad` | nothing | yes | ApplyGuy 2027-New-Grad-Jobs (GitHub): mostly U.S. new-grad/entry-level SWE roles, last 60 days, ~15 min refresh |
| `arbeitnow` | nothing | yes | Arbeitnow job board API: ATS-sourced jobs, mostly Europe/Germany, with full descriptions, hourly |
| `authenticjobs` | nothing | yes | Authentic Jobs RSS: US developer and design roles |
| `coconight_na_newgrad` | nothing | yes | coconight01 2027 North America New Grad Jobs (GitHub): ~3k merged new-grad roles, daily |
| `cryptojobslist` | nothing | yes | CryptoJobsList RSS: web3, crypto and fintech roles |
| `derec4_newgrad` | nothing | yes | DereC4 internships-and-newgrad (GitHub): re-aggregated new-grad roles from Zapply, Simplify and others; last ~month only |
| `dreamworkhq_newgrad_swe` | nothing | yes | dreamworkhq New-Grad-Software-Engineer-Jobs (GitHub): entry-level US SWE/data/security roles crawled from career pages, daily, with salary; links go to dreamwor |
| `findwork` | FINDWORK_API_KEY | no | Findwork.dev: tech jobs aggregated from HN, YC, Remote OK, WeWorkRemotely and more (API key) |
| `google_careers` | nothing | yes | Google Careers XML feed: every open Google, YouTube and DeepMind role, full descriptions |
| `harrycodingnow_newgrad` | nothing | yes | harrycodingnow new-grad-2027-tracker (GitHub): bot-scraped 2027 new-grad SWE/AI roles in the US, full text + direct ATS links, updated every few hours |
| `himalayas` | nothing | yes | Himalayas remote-jobs API: keyword search, US-open remote roles with full descriptions |
| `hirly_newgrad_swe` | nothing | yes | hirly New Grad Software Engineer Jobs (hirly.me / GitHub): newest 250 new-grad/junior SWE roles, daily |
| `hn_jobstories` | nothing | yes | Hacker News job stories (news.ycombinator.com/jobs): the 'X (YC S19) Is Hiring' posts |
| `hn_whoishiring` | nothing | yes | Hacker News 'Ask HN: Who is hiring?' monthly thread (via Algolia HN API), one listing per company comment |
| `jobdataapi` | nothing | yes | jobdataapi.com: ATS-sourced postings from 80,000+ companies, newest first, title search. |
| `jobicy` | nothing | yes | Jobicy remote jobs API: remote software/data/DevOps/QA/security roles from the last 7 days |
| `jobright_swe_newgrad` | nothing | yes | Jobright.ai 2026-Software-Engineer-New-Grad (GitHub): new-grad SWE roles from the last ~7 days, several updates a day; links are jobright.ai pages |
| `jobspresso` | nothing | yes | Jobspresso RSS: curated remote roles |
| `jooble` | JOOBLE_API_KEY | no | Jooble (job search aggregator): US postings from many sites via the REST API; snippets only (unverified, off by default) |
| `jsearch` | RAPIDAPI_KEY | no | JSearch (RapidAPI): Google for Jobs results; free plan 200 requests/month |
| `larajobs` | nothing | yes | LaraJobs RSS: PHP/Laravel roles |
| `microsoft_careers` | nothing | yes | Microsoft Careers (apply.careers.microsoft.com): every Microsoft opening, newest first |
| `nodesk` | nothing | yes | NoDesk RSS: curated remote roles |
| `python_jobs` | nothing | yes | Python.org job board RSS |
| `quantscraper_quant_newgrad` | nothing | yes | QuantScraper Quant-Internships-New-Grad-2027 (GitHub): graduate roles at ~50 quant firms, worldwide |
| `remotefirstjobs` | nothing | yes | RemoteFirstJobs RSS: high-volume remote roles |
| `remoteok` | nothing | yes | Remote OK (remoteok.com API): the ~100 newest remote jobs worldwide, with full descriptions |
| `remotive` | nothing | yes | Remotive public API: remote-only tech jobs with full descriptions (24h delayed, small set) |
| `resumax_newgrad_tech` | nothing | yes | ResuMax new-grad-tech-jobs (GitHub): global new-grad SWE/data/security/infra/hardware roles, last 30 days, direct ATS links |
| `rubyonrails_jobs` | nothing | yes | Ruby on Rails job board RSS |
| `serpapi_google_jobs` | SERPAPI_API_KEY | no | Google for Jobs via SerpApi: postings from nearly every ATS, company site and board, one search per query (paid API, off by default) |
| `simplify_newgrad` | nothing | yes | SimplifyJobs New-Grad-Positions (GitHub): curated new-grad SWE/data/quant/PM roles, daily |
| `speedyapply_ai_newgrad` | nothing | yes | SpeedyApply 2027-AI-College-Jobs (GitHub): USA new-grad AI/ML roles, FAANG+/Quant/Other |
| `speedyapply_swe_newgrad` | nothing | yes | SpeedyApply 2027-SWE-College-Jobs (GitHub): USA new-grad SWE roles, FAANG+/Quant/Other |
| `swejoblists_newgrad` | nothing | yes | swejoblists.com 2027 new-grad list (GitHub mirror): up to 1,000 roles linking swejoblists pages (describe() adds the posting text and employer apply link); nois |
| `tarive_ml_newgrad` | nothing | yes | Tar-ive ML_Newgrad_2027 (GitHub): merged new-grad ML/DS/SWE-ML/quant roles in the U.S., hourly |
| `theirstack` | THEIRSTACK_API_KEY | yes | TheirStack API: junior postings aggregated from career sites, ATSs and boards (US, newest first). |
| `themuse` | THEMUSE_API_KEY | yes | The Muse public jobs API: Entry Level software/data/IT postings across US employers |
| `usajobs` | USAJOBS_API_KEY, USAJOBS_EMAIL | no | USAJOBS (OPM): US federal civilian jobs via the official search API; mostly U.S. citizens only |
| `vansh_newgrad2027` | nothing | yes | vanshb03/New-Grad-2027 (GitHub): curated 2026/2027 new-grad SWE/data/quant/PM roles, US/Canada/remote |
| `vuejobs` | nothing | yes | VueJobs RSS: Vue/Nuxt frontend and full-stack roles |
| `weworkremotely` | nothing | no | We Work Remotely (RSS): remote programming, back-end, front-end, full-stack and DevOps roles |
| `workingnomads` | nothing | yes | Working Nomads public JSON feed: latest remote Development postings (a few dozen, one request) |
| `workopia_us_newgrad` | nothing | yes | workopia/US-New-Grad-Internship-Jobs (GitHub): daily US graduate and entry-level tech/data roles |
| `zapply_newgrad2027` | nothing | yes | Zapply New-Grad 2027 lists (GitHub): recent new-grad SWE, AI/ML, security, systems roles; ~10-min updates |

### Company boards (one company per call, found automatically from links)

| ATS | Example board | Full text |
|---|---|---|
| `adp_workforce_now` | `89da4960-4d45-4b46-b7aa-5959c5f71827` | per posting (describe) |
| `applicantpro` | `karmak` | per posting (describe) |
| `ashby` | `ramp` | in the list |
| `avature` | `bloomberg` | per posting (describe) |
| `bamboohr` | `kiln` | per posting (describe) |
| `breezy` | `vagaro` | per posting (describe) |
| `comeet` | `team8:61.003` | in the list |
| `eightfold` | `netflix:netflix.com` | per posting (describe) |
| `factorial` | `careers` | per posting (describe) |
| `freshteam` | `talent360` | per posting (describe) |
| `gem_ats` | `linktree` | in the list |
| `greenhouse` | `figma` | in the list |
| `hibob` | `aerocloudsystems` | in the list |
| `hireology` | `hireology2` | in the list |
| `homerun` | `moneybird` | in the list |
| `icims` | `careers-gdms` | per posting (describe) |
| `jazzhr` | `snapsheet` | in the list |
| `jobdataapi` | `walgreens.com` | in the list |
| `jobscore` | `matrixspace` | in the list |
| `join_com` | `bexio` | per posting (describe) |
| `lever` | `palantir` | in the list |
| `lever_eu` | `quantinuum` | in the list |
| `manatal` | `manatal` | in the list |
| `oracle_hcm` | `eeho:us2:CX_45001` | per posting (describe) |
| `paylocity` | `e9d022bb-1790-488d-a784-e537639793d5` | per posting (describe) |
| `personio` | `1komma5grad` | in the list |
| `phenom` | `careers.mastercard.com:us:en` | per posting (describe) |
| `pinpoint` | `coforma` | in the list |
| `polymer` | `themis` | per posting (describe) |
| `radancy_talentbrew` | `jobs.intuit.com` | per posting (describe) |
| `recruitee` | `bunq` | in the list |
| `rippling` | `boom-supersonic` | per posting (describe) |
| `smartrecruiters` | `BoschGroup` | per posting (describe) |
| `successfactors` | `career4.successfactors.com:C0002633795P` | in the list |
| `taleo` | `hdr:ex` | per posting (describe) |
| `teamtailor` | `career` | in the list |
| `theirstack` | `stripe.com` | in the list |
| `trakstar_hire` | `zeitview` | in the list |
| `ukg_ultipro` | `recruiting.ultipro.com:rop1001roper:cf088029-453b-46f8-bcab-` | per posting (describe) |
| `workable` | `tickpick` | per posting (describe) |
| `workday` | `salesforce:wd12:External_Career_Site` | per posting (describe) |
| `zoho_recruit` | `delsys` | in the list |

Not used, on purpose: Amazon and Apple (their terms forbid automated collection), Jobvite (no public
feed), Dover (terms forbid it), Careerjet (publisher accounts only), Indeed (API retired), LinkedIn (forbids
automated access; use screenshots).
