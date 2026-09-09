"""Validate the historical operator-state import without retaining the workbook or database."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from job_scout.history import explicit_blacklist_evidence, historical_records, workbook_sha256
from job_scout.storage.sqlite import SQLiteRepository
from job_scout.target_universe import build_target_universe, read_historical_links

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
UNIVERSE = json.loads(
    (ROOT.parent / "target_universe_v1" / "historical_v1.json").read_text(encoding="utf-8")
)


def _logical_universe_matches(links) -> bool:
    rebuilt = build_target_universe(
        links,
        input_provenance={"validation_only": True},
        generated_at="validation-only",
    ).model_dump(mode="json")
    for field in (
        "total_historical_rows",
        "historical_status_counts",
        "classification_counts",
        "recognized_rows_by_source",
        "target_counts_by_source",
        "target_occurrence_distribution",
        "target_records",
        "unresolved",
    ):
        if rebuilt[field] != UNIVERSE[field]:
            return False
    return True


def validate(*, workbook: Path, database: Path, client_id: str) -> dict[str, object]:
    links = read_historical_links(workbook)
    records = historical_records(workbook)
    blacklist = explicit_blacklist_evidence(workbook)
    checksum = workbook_sha256(workbook)
    repository = SQLiteRepository(database)
    inserted, already_present = repository.import_historical_records(
        client_id=client_id,
        workbook_sha256=checksum,
        records=records,
        blacklist_evidence=blacklist,
    )
    statuses = Counter(record.operator_status for record in records)
    supported = [record for record in records if record.source_job_id is not None]
    with repository.connect() as connection:
        duplicate_urls = connection.execute(
            "SELECT COUNT(*) FROM (SELECT normalized_url FROM historical_job_links "
            "WHERE client_id=? GROUP BY normalized_url HAVING COUNT(*) > 1)",
            (client_id,),
        ).fetchone()[0]
        duplicate_identities = connection.execute(
            "SELECT COUNT(*) FROM (SELECT source, source_board_id, source_job_id "
            "FROM historical_job_links WHERE client_id=? AND source_job_id IS NOT NULL "
            "GROUP BY source, source_board_id, source_job_id HAVING COUNT(*) > 1)",
            (client_id,),
        ).fetchone()[0]
    return {
        "actual_workbook_sha256": checksum,
        "client_id": client_id,
        "duplicate_normalized_urls": duplicate_urls,
        "duplicate_supported_source_identities": duplicate_identities,
        "explicit_blacklist_evidence": {
            "company_entries": sum(entry.kind == "company" for entry in blacklist),
            "note_entries": sum(entry.kind == "note" for entry in blacklist),
            "sheets": sorted({entry.source_sheet for entry in blacklist}),
        },
        "import_already_present": already_present,
        "inserted": inserted,
        "logical_target_universe_matches_committed_v1": _logical_universe_matches(links),
        "malformed_or_unusable": sum(
            not record.normalized_url.startswith(("http://", "https://")) for record in records
        ),
        "operator_verified_workbook_sha256": MANIFEST["operator_verified_workbook_sha256"],
        "row_counts": {
            "applied": statuses["applied"],
            "not_applied": statuses["not_applied"],
            "total": len(records),
            "unknown": statuses["unknown"],
        },
        "supported_source_identities": len(supported),
        "target_universe_v1_workbook_sha256": MANIFEST["target_universe_v1_workbook_sha256"],
        "url_only_records": len(records) - len(supported),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            validate(workbook=args.workbook, database=args.database, client_id=args.client),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
