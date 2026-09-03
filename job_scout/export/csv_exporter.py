from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from job_scout.dedupe.resolver import select_preferred_url
from job_scout.domain.models import Job

CSV_COLUMNS = ["Job Title", "Company Name", "Job Link", "Job Description", "Job Platform"]


def export_row(job: Job) -> dict[str, str]:
    return {
        "Job Title": job.title,
        "Company Name": job.company,
        "Job Link": select_preferred_url(job),
        "Job Description": job.description_text or "",
        "Job Platform": job.source.title(),
    }


def write_csv(path: str | Path, jobs: Iterable[Job]) -> int:
    rows = [export_row(job) for job in jobs]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    exists = destination.exists()
    if exists:
        with destination.open(newline="", encoding="utf-8") as handle:
            if next(csv.reader(handle), None) != CSV_COLUMNS:
                raise ValueError("existing CSV header does not match the export contract")
        if not rows:
            return 0
    with destination.open("a" if exists else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)
