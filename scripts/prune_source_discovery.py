"""Bounded retention for stale, never-admitted source-discovery candidates."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from job_scout.storage.factory import create_repository
from job_scout.storage.source_discovery import SourceDiscoveryStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repository = create_repository(args.database)
    result = SourceDiscoveryStore(repository).prune_inactive_targets(
        now=datetime.now(UTC),
        retention_days=args.retention_days,
        batch_size=args.batch_size,
    )
    payload = json.dumps(result, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
