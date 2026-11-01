"""Import your existing tracker CSV into the database. Safe to re-run: rows already
imported (same content) are skipped, so you can re-export the sheet and import again."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from appa.core import jobid, normalize
from appa.db import STATUSES, Application

# Your sheet's headers -> database fields.
DEFAULT_COLUMNS = {
    "company": "company",
    "Application Status": "status",
    "Stage": "stage",
    "Position": "role",
    "Portal": "link",
    "Location": "location",
    "Date of Application": "date_applied",
    "Referal": "referral",
    "People": "people",
    "todo": "todo",
    "Notes": "notes",
}
_STATUS_LOOKUP = {s.lower(): s for s in STATUSES}


@dataclass
class Report:
    imported: int = 0
    skipped_existing: int = 0
    warnings: list[str] = field(default_factory=list)


def _clean(v: str | None) -> str | None:
    """Strip a CSV cell; blank or 'none' becomes None."""
    v = (v or "").strip()
    return None if v == "" or v.lower() == "none" else v


def _row_key(raw: dict) -> str:
    """A hash of the whole CSV row, so re-importing the same file skips rows already imported."""
    blob = "\x1f".join(f"{k}={(raw.get(k) or '').strip()}" for k in sorted(raw))
    return hashlib.sha256(blob.encode()).hexdigest()


def import_csv(
    s: Session, path: Path, applied_with_email: str | None, columns: dict[str, str] = DEFAULT_COLUMNS
) -> Report:
    """Import a tracker CSV as applications, skipping imported rows and duplicate job IDs; commits and reports."""
    rep = Report()
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [h for h in columns if h not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"CSV is missing expected columns: {missing}")
        rows = list(reader)

    existing = set(s.scalars(select(Application.import_key).where(Application.import_key.is_not(None))))
    job_keys: dict[str, str] = {
        k: f"existing #{i}"
        for k, i in s.execute(select(Application.job_key, Application.id).where(Application.job_key.is_not(None)))
    }
    for line, raw in enumerate(rows, start=2):  # line 1 is the header
        key = _row_key(raw)
        if key in existing:
            rep.skipped_existing += 1
            continue
        vals = {field: _clean(raw.get(header)) for header, field in columns.items()}
        if not vals.get("company"):
            rep.warnings.append(f"line {line}: no company, skipped")
            continue

        key_id = jobid.job_key(vals.get("link"))
        if key_id and key_id in job_keys:
            # The job ID is the posting's identity: a second row with it is the same job.
            rep.warnings.append(f"line {line}: same job ID as {job_keys[key_id]} ({key_id}), skipped")
            continue

        status = vals.get("status")
        if status and status.lower() not in _STATUS_LOOKUP:
            rep.warnings.append(f"line {line}: unknown status {status!r}, stored as-is")
        elif status:
            status = _STATUS_LOOKUP[status.lower()]
        elif not status:
            rep.warnings.append(f"line {line}: {vals['company']} has no status")

        try:
            applied_on = normalize.parse_date(vals.get("date_applied"))
        except ValueError as e:
            rep.warnings.append(f"line {line}: {e}, left blank")
            applied_on = None
        if status in ("Applied", "Rejected", "Ghosted") and not applied_on:
            rep.warnings.append(f"line {line}: {vals['company']} has no application date")

        ref = (vals.get("referral") or "").lower()
        note_id = None
        if not key_id:  # no ID in the link: use one written in your notes, if exactly one
            found = jobid.ids_in_text(vals["company"], vals.get("role"), vals.get("notes"))
            candidate = jobid.resolve_key(vals.get("link"), found[0], vals["company"]) if len(found) == 1 else None
            if candidate and candidate not in job_keys:
                note_id = found[0]
                job_keys[candidate] = f"line {line}"
        s.add(
            Application(
                company=vals["company"],
                role=vals.get("role") or "",
                status=status,
                stage=vals.get("stage"),
                link=vals.get("link"),
                location=vals.get("location"),
                date_applied=applied_on,
                applied_with_email=applied_with_email if status else None,
                referral={"yes": True, "no": False}.get(ref),
                people=vals.get("people"),
                todo=vals.get("todo"),
                notes=vals.get("notes"),
                source="csv-backfill",
                import_key=key,
                job_id=note_id,
                job_id_source="notes" if note_id else None,
            )
        )
        existing.add(key)
        if key_id:
            job_keys[key_id] = f"line {line}"
        rep.imported += 1
    s.commit()
    return rep


def name_warnings(s: Session) -> list[str]:
    """Companies whose spelling differs only slightly from another, so you can fix typos."""
    from difflib import SequenceMatcher

    names: dict[str, str] = {}
    for a in s.scalars(select(Application)):
        names.setdefault(a.company_norm, a.company.strip())
    keys = sorted(names)
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if len(a) > 3 and SequenceMatcher(None, a, b).ratio() >= 0.85:
                out.append(f"{names[a]!r} vs {names[b]!r}")
    return out


def ids_from_notes(s: Session, apply: bool = False) -> list[tuple[int, str, str, str]]:
    """Rows with no identity whose own text names exactly one job ID. Returns
    (row id, company, job id, outcome) and, with apply=True, saves them."""
    out = []
    taken = set(s.scalars(select(Application.job_key).where(Application.job_key.is_not(None))))
    taken_ids = {k.partition(":")[2].rpartition(":")[2] for k in taken}  # the bare ID, on any site
    for a in s.scalars(select(Application).where(Application.job_key.is_(None)).order_by(Application.id)):
        found = jobid.ids_in_text(a.company, a.role, a.notes)
        if len(found) != 1:
            if len(found) > 1:
                out.append((a.id, a.company, ", ".join(found), "skipped: more than one ID in the text"))
            continue
        key = jobid.resolve_key(a.link, found[0], a.company)
        if not key:
            continue
        if key in taken or found[0] in taken_ids:
            # e.g. a note saying "separate from the other posting 563980770505837"
            out.append((a.id, a.company, found[0], "skipped: another row already has this ID"))
            continue
        out.append((a.id, a.company, found[0], "set" if apply else "would set"))
        if apply:
            a.job_id, a.job_id_source = found[0], "notes"
            taken.add(key)
    if apply:
        s.commit()
    return out
