"""Build Target Universe V1 from an offline historical workbook or CSV export."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from job_scout.target_universe import (
    build_target_universe,
    read_historical_links,
    utc_timestamp,
    write_universe,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_report(universe: dict[str, object]) -> str:
    counts = universe["classification_counts"]
    targets = universe["target_counts_by_source"]
    distributions = universe["target_occurrence_distribution"]
    total = int(universe["total_historical_rows"])
    supported = int(counts.get("supported_target", 0))
    records = universe["target_records"]
    top = sorted(
        records,
        key=lambda item: (-item["historical_occurrence_count"], item["target_identity"]),
    )[:20]
    lines = [
        "# Target Universe V1",
        "",
        "This is an offline historical-evidence artifact. A historical occurrence does not prove a target is active.",
        "",
        "## Corpus reconciliation",
        "",
        f"- Historical link rows: {total}",
        f"- Safely derivable four-source rows: {supported} ({supported / total:.1%})",
        f"- Outside current four-source contracts: {total - supported} ({(total - supported) / total:.1%})",
        "",
        "## Source targets",
        "",
        "| Source | Recognized rows | Canonical targets | Occurrences per target (min / median / max) |",
        "| --- | ---: | ---: | ---: |",
    ]
    recognized = universe["recognized_rows_by_source"]
    for source in ("greenhouse", "ashby", "workday", "lever"):
        distribution = distributions.get(source, {})
        lines.append(
            f"| {source} | {recognized.get(source, 0)} | {targets.get(source, 0)} | "
            f"{distribution.get('min')} / {distribution.get('median')} / {distribution.get('max')} |"
        )
    lines.extend(["", "## Classification", ""])
    for key, value in sorted(counts.items()):
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Top historical targets", ""])
    for item in top:
        lines.append(
            f"- `{item['target_identity']}` — {item['historical_occurrence_count']} occurrences "
            f"across {item['distinct_historical_url_count']} distinct URLs"
        )
    lines.extend(
        [
            "",
            "## Relationship to sourcing",
            "",
            (
                "SearchBrief defines desired jobs. This universe records known source targets. "
                "A SourcingPlan selects a bounded target set for one operation. Health is intentionally unverified here."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--input-url")
    parser.add_argument("--input-modified-at")
    parser.add_argument("--generated-at")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.input.resolve()
    links = read_historical_links(source)
    provenance = {
        "source_filename": source.name,
        "sha256": file_sha256(source),
        "url": args.input_url,
        "modified_at": args.input_modified_at,
    }
    universe = build_target_universe(
        links, input_provenance=provenance, generated_at=args.generated_at or utc_timestamp()
    )
    write_universe(args.output, universe)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_report(universe.model_dump(mode="json")), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "rows": universe.total_historical_rows,
                "target_counts_by_source": universe.target_counts_by_source,
                "classification_counts": universe.classification_counts,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
