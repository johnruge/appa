"""One job in, one verdict out: dedupe -> pre-filter -> extract -> rules -> record."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from appa import db, extract, observe
from appa.config import Config
from appa.core import jobid, rules
from appa.core.models import Decision, Extraction, Job, Verdict

if TYPE_CHECKING:
    from appa.runtime.context import RunContext

# Called for jobs that pass every rule: returns the final decision (e.g. "you apply yourself").
Router = Callable[[Job, Decision], Decision]

STATUS_FOR = {
    Verdict.CANDIDATE: "To Apply",
    Verdict.MANUAL: "Apply Myself",
    Verdict.REVIEW: "Needs Review",
}


@dataclass
class Result:
    job: Job
    decision: Decision
    skipped: bool = False  # already evaluated on an earlier run
    application_id: int | None = None  # the application this created, if any


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        session: Session | None = None,
        model: str | None = None,
        ctx: RunContext | None = None,
        router: Router | None = None,
        keyless: bool = False,
    ):
        """A screening pipeline using the given policy, with its own session unless one is passed."""
        self.cfg = cfg
        self.model = model  # None = the screener default in config/models.yaml
        self.ctx = ctx  # inside a run: model calls are charged to its budget and recorded
        self.router = router
        self.keyless = keyless  # no model key: keyword screen (never rejects; red flags go to review)
        self.s = session or db.sessions()()

    def seen(self, key: str) -> bool:
        """True if a posting with this key was already evaluated."""
        return self.s.get(db.SeenJob, key) is not None

    def _dedupe(self, job: Job) -> tuple[Decision | None, list[str]]:
        """Returns (final decision if this is a duplicate, notes to carry forward).

        Only a matching job ID makes a duplicate. Applications on file with no link or
        no job ID never block or flag anything.
        """
        m = db.find_match(self.s, job.company, job.title, job.url, job.posting_id)
        if not m:
            return None, []
        a = m.app
        if m.kind == "same_posting":
            label = f"#{a.id} {a.company} / {a.role} ({a.status or 'no status'})"
            return Decision(verdict=Verdict.DUPLICATE, reasons=[f"already tracked (same job ID): {label}"]), []
        if m.kind == "company_applied":
            roles = "; ".join(f"{o.role} ({o.status})" for o in m.others if o.job_key)[:300]
            note = f"other postings you applied to at {a.company}: {roles}"
            if self.cfg.rules.same_company == "skip":
                return Decision(verdict=Verdict.DUPLICATE, reasons=[note]), []
            return None, [note]
        return None, []

    def _identify(self, job: Job) -> None:
        """No job ID in the link or page markup: ask a model to find one on the page (verified
        verbatim). Without it, the posting is identified by its link."""
        if job.key != job.url or job.posting_id:
            return
        jid, meta = extract.identify_job_id(job, self.model)
        if meta and self.ctx:
            self.ctx.charge(meta, purpose=f"find job ID: {job.company}"[:120])
        if jid:
            job.posting_id, job.posting_id_source = jid, "ai"

    def _observe_id(self, job: Job) -> None:
        """Record links whose job ID no pattern read, and where the ID came from instead."""
        if not job.url.startswith(("http://", "https://")) or jobid.job_key(job.url):
            return
        observe.record(
            self.s,
            "id_not_in_link",
            observe.path_shape(job.url),
            url=job.url,
            detail={"url": job.url, "found_id": job.posting_id, "id_source": job.posting_id_source or "none"},
        )

    def process(self, job: Job, force: bool = False) -> Result:
        """Dedupe, filter and screen one job, record it (and an application if it qualifies), and commit."""
        if not self.keyless:  # keyword screening makes no model calls at all
            self._identify(job)
        if not force and self.seen(job.key):
            return Result(
                job, Decision(verdict=Verdict.DUPLICATE, reasons=["evaluated on an earlier run"]), skipped=True
            )

        self._observe_id(job)
        decision, flags = self._dedupe(job)
        if not decision:
            title_hits = rules.title_rejects(job.title, self.cfg.rules)
            if title_hits:
                decision = Decision(verdict=Verdict.REJECT, reasons=title_hits)

        ex: Extraction | None = None
        if not decision and self.keyless:
            if self.ctx:
                self.ctx.check()
            decision = rules.keyword_screen(job, self.cfg.rules)
            if rules.on_manual_list(job.company, self.cfg.rules) and decision.verdict != Verdict.REJECT:
                decision = Decision(verdict=Verdict.MANUAL, reasons=["on your apply-myself list", *decision.reasons])
            elif decision.verdict == Verdict.CANDIDATE and self.router:
                decision = self.router(job, decision)
        if not decision:
            if self.ctx:
                self.ctx.check()
            try:
                ex, meta = extract.extract_with_meta(job, self.model)
                if self.ctx:
                    self.ctx.charge(meta, purpose=f"screen: {job.company} / {job.title}"[:120])
            except extract.ExtractionError as e:
                if self.ctx:
                    self.ctx.charge(cost_usd=e.cost_usd, purpose="screen", error=str(e))
                if e.fatal:
                    self.s.rollback()
                    raise
                observe.record(
                    self.s,
                    "extract_error",
                    str(e)[:200],
                    url=job.url,
                    detail={"company": job.company, "title": job.title},
                )
                decision = Decision(verdict=Verdict.REVIEW, reasons=[f"could not evaluate: {e}"])
            else:
                if job.ats == "unknown":  # page metadata is unreliable; trust the posting text
                    job.company, job.title = ex.company or job.company, ex.title or job.title
                    job.location = job.location or ex.location
                    decision, more = self._dedupe(job)
                    flags += [f for f in more if f not in flags]
                elif ex.company and job.company == job.company.lower():
                    job.company = ex.company  # Lever/Ashby only give us the URL slug
                decision = decision or rules.decide(job, ex, self.cfg.rules, self.cfg.candidate)
                if decision.verdict == Verdict.CANDIDATE and self.router:
                    decision = self.router(job, decision)

        if flags and decision.verdict == Verdict.CANDIDATE and self.cfg.rules.same_company == "flag":
            decision = Decision(verdict=Verdict.REVIEW, reasons=[*flags, *decision.reasons])
        elif flags and decision.verdict in (Verdict.CANDIDATE, Verdict.MANUAL, Verdict.REVIEW):
            decision.reasons += flags  # informational only

        app_id = None
        if decision.verdict in STATUS_FOR:
            a = db.Application(
                company=job.company,
                role=job.title,
                location=job.location or None,
                link=job.url,
                status=STATUS_FOR[decision.verdict],
                source=f"pipeline:{job.source}",
                job_id=job.posting_id,
                job_id_source=job.posting_id_source,
                notes="; ".join(decision.reasons),
            )
            self.s.add(a)
            self.s.flush()
            app_id = a.id
        self.s.merge(
            db.SeenJob(
                key=job.key,
                company=job.company,
                title=job.title,
                url=job.url,
                source=job.source,
                verdict=decision.verdict.value,
                reasons=decision.reasons,
                extraction=ex.model_dump() if ex else None,
                application_id=app_id,
                run_id=self.ctx.run_id if self.ctx else None,
            )
        )
        self.s.commit()
        return Result(job, decision, application_id=app_id)
