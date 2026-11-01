"""Screening eval: run a model over labeled postings, score its extraction and the
resulting verdict, and report accuracy, the costly mistakes, and real cost.

    appa eval                                    # default screener model, all cases
    appa eval --model a --model b                # compare models side by side
    appa eval snapshot <url>                     # save a real posting to evals/local/ to label

Cases: evals/screening/*.yaml (synthetic, committed) and evals/local/*.yaml (your real
postings, gitignored). Results: evals/results/*.json (gitignored).
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from appa import extract
from appa.config import ROOT, Candidate, Rules
from appa.core.models import Job, Verdict
from appa.core.rules import decide

CASE_DIRS = [ROOT / "evals" / "screening", ROOT / "evals" / "local"]
RESULTS_DIR = ROOT / "evals" / "results"

# Fixed policy so scores mean the same thing on every machine (not your personal profile).
EVAL_CANDIDATE = Candidate(
    name="eval", application_email="eval@example.com", needs_sponsorship_now=False, needs_sponsorship_future=True
)
EVAL_RULES = Rules(max_years_experience=2, manual_companies=[], title_excludes=[], title_includes=[])

FIELDS = (
    "sponsorship",
    "citizenship_required",
    "clearance_required",
    "min_years_experience",
    "is_entry_level",
    "staffing_agency",
    "suspicious",
)


@dataclass
class Case:
    id: str
    company: str
    title: str
    posting: str
    expect: dict
    source: str = ""


@dataclass
class CaseResult:
    id: str
    ok: bool = True
    verdict: str = ""
    expected_verdict: list[str] = field(default_factory=list)
    mismatches: dict[str, tuple] = field(default_factory=dict)  # field -> (expected, got)
    error: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    served_by: str = ""


@dataclass
class Report:
    model: str
    cases: int = 0
    verdict_correct: int = 0
    false_rejects: list[str] = field(default_factory=list)  # would lose a job you could get
    missed_rejects: list[str] = field(default_factory=list)  # would auto-apply to a no-go job
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    results: list[CaseResult] = field(default_factory=list)
    stopped_early: bool = False


def load_cases(dirs: list[Path] = CASE_DIRS, only: list[str] | None = None) -> list[Case]:
    """Load the labeled eval cases (unlabeled snapshots skipped), optionally only some ids; exits on duplicate ids."""
    cases: list[Case] = []
    for d in dirs:
        for path in sorted(d.glob("*.yaml")) if d.exists() else []:
            data = yaml.safe_load(path.read_text()) or []
            for raw in data if isinstance(data, list) else [data]:
                if not raw.get("expect"):
                    continue  # unlabeled snapshot
                cases.append(
                    Case(
                        raw["id"],
                        raw.get("company", ""),
                        raw.get("title", ""),
                        raw["posting"],
                        raw["expect"],
                        source=str(path.relative_to(ROOT)),
                    )
                )
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate eval case ids: {sorted(dupes)}")
    return [c for c in cases if not only or c.id in only]


def _observed(ex) -> dict:
    """The extraction fields the evals compare, keyed by their expect: names."""
    return {
        "sponsorship": ex.sponsorship,
        "citizenship_required": ex.citizenship_required,
        "clearance_required": ex.clearance_required,
        "min_years_experience": ex.min_years_experience,
        "is_entry_level": ex.is_entry_level,
        "staffing_agency": ex.is_staffing_agency,
        "suspicious": bool(ex.suspicious_instructions),
    }


def score_case(case: Case, model: str | None) -> CaseResult:
    """Extract one case with the model and record field and verdict mismatches, cost and latency."""
    res = CaseResult(case.id)
    exp_v = case.expect.get("verdict")
    res.expected_verdict = [exp_v] if isinstance(exp_v, str) else list(exp_v or [])
    job = Job(company=case.company, title=case.title, url=f"https://eval.invalid/{case.id}", description=case.posting)
    try:
        ex, meta = extract.extract_with_meta(job, model)
    except extract.ExtractionError as e:
        res.ok, res.error, res.cost_usd = False, str(e)[:300], e.cost_usd
        return res
    res.cost_usd, res.latency_s, res.served_by = meta.cost_usd, meta.latency_s, meta.served_by
    got = _observed(ex)
    for f in FIELDS:
        if f in case.expect and case.expect[f] != got[f]:
            res.mismatches[f] = (case.expect[f], got[f])
    res.verdict = decide(job, ex, EVAL_RULES, EVAL_CANDIDATE).verdict.value
    if res.expected_verdict and res.verdict not in res.expected_verdict:
        res.mismatches["verdict"] = (res.expected_verdict, res.verdict)
    res.ok = not res.mismatches
    return res


def run(model: str | None, cases: list[Case], *, workers: int = 4, max_cost: float = 1.0) -> Report:
    """Score the cases in parallel with one model, stopping once max_cost is spent; tallies accuracy and misses."""
    from appa.llm import registry as models

    name = models.get_registry().get("screener", model).name  # fails fast: unknown model, no API key
    rep = Report(model=name)
    spent = 0.0
    lock = threading.Lock()
    stop = threading.Event()

    def one(case: Case) -> CaseResult | None:
        """Score one case unless the cost limit was already hit; adds its cost to the shared total."""
        nonlocal spent
        if stop.is_set():
            return None
        r = score_case(case, name)
        with lock:
            spent += r.cost_usd
            if spent >= max_cost:
                stop.set()
        return r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [r for r in pool.map(one, cases) if r is not None]
    rep.stopped_early = len(results) < len(cases)

    by_id = {c.id: c for c in cases}
    for r in results:
        case = by_id[r.id]
        rep.results.append(r)
        rep.cases += 1
        rep.cost_usd += r.cost_usd
        rep.latency_s += r.latency_s
        if r.error:
            rep.errors += 1
            continue
        for f in FIELDS:
            if f in case.expect:
                rep.field_total[f] = rep.field_total.get(f, 0) + 1
                rep.field_correct[f] = rep.field_correct.get(f, 0) + (f not in r.mismatches)
        if r.expected_verdict:
            if r.verdict in r.expected_verdict:
                rep.verdict_correct += 1
            elif r.verdict == Verdict.REJECT.value:
                rep.false_rejects.append(r.id)
            elif Verdict.REJECT.value in r.expected_verdict and r.verdict in ("candidate", "manual"):
                rep.missed_rejects.append(r.id)
    return rep


def save(rep: Report) -> Path:
    """Write a report as timestamped JSON in the results directory; returns its path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{stamp}_{re.sub(r'[^a-z0-9]+', '-', rep.model.lower())}.json"
    path.write_text(json.dumps(asdict(rep), indent=2, default=str))
    return path


def format_reports(reports: list[Report]) -> str:
    """A text table comparing reports: verdict accuracy, false/missed rejects, cost, field accuracy and misses."""
    lines = []
    head = (
        f"{'model':<42} {'verdict':>8} {'false rej':>9} {'missed rej':>10} {'errors':>6} "
        f"{'cost':>9} {'$/case':>8} {'avg s':>6}"
    )
    lines += [head, "-" * len(head)]
    for r in reports:
        n_v = sum(1 for x in r.results if x.expected_verdict and not x.error)
        acc = f"{r.verdict_correct}/{n_v}" if n_v else "-"
        per = r.cost_usd / r.cases if r.cases else 0
        avg = r.latency_s / max(1, r.cases - r.errors)
        lines.append(
            f"{r.model:<42} {acc:>8} {len(r.false_rejects):>9} {len(r.missed_rejects):>10} "
            f"{r.errors:>6} {r.cost_usd:>9.5f} {per:>8.5f} {avg:>6.1f}"
        )
    lines.append("")
    lines.append("field accuracy:")
    for r in reports:
        parts = [f"{f} {r.field_correct.get(f, 0)}/{r.field_total[f]}" for f in FIELDS if r.field_total.get(f)]
        lines.append(f"  {r.model}: " + ", ".join(parts))
    for r in reports:
        bad = [x for x in r.results if not x.ok]
        if bad:
            lines.append(f"\nmisses for {r.model}:")
            for x in bad:
                if x.error:
                    lines.append(f"  {x.id}: ERROR {x.error}")
                else:
                    detail = "; ".join(f"{k}: expected {e!r}, got {g!r}" for k, (e, g) in x.mismatches.items())
                    lines.append(f"  {x.id}: {detail}")
        if r.stopped_early:
            lines.append(f"  (stopped early: hit the --max-cost limit after {r.cases} cases)")
    return "\n".join(lines)


SNAPSHOT_TEMPLATE = """# Real posting saved for your eval. Stays local (evals/local/ is gitignored).
# Fill in `expect` with what a correct screener should conclude, then run `appa eval`.
# Delete any expect line you don't want scored.
id: {id}
company: {company}
title: {title}
url: {url}
expect:
  # verdict: candidate        # candidate | review | reject | manual
  # sponsorship: not_mentioned
  # citizenship_required: false
  # clearance_required: false
  # min_years_experience: null
  # is_entry_level: true
posting: |
{posting}
"""


def snapshot(job: Job, case_id: str | None = None) -> Path:
    """Save a posting as an unlabeled eval case YAML under evals/local; returns its path."""
    out = ROOT / "evals" / "local"
    out.mkdir(parents=True, exist_ok=True)
    cid = case_id or re.sub(r"[^a-z0-9]+", "-", f"{job.company}-{job.title}".lower()).strip("-")[:60]
    path = out / f"{cid}.yaml"
    body = "\n".join("  " + line for line in job.description.splitlines())
    path.write_text(
        SNAPSHOT_TEMPLATE.format(
            id=cid, company=json.dumps(job.company), title=json.dumps(job.title), url=json.dumps(job.url), posting=body
        )
    )
    return path
