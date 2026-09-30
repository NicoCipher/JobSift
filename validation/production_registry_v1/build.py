"""Rebuild the production-approved ATS registry from committed validation evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_scout.production_registry import build_production_registry

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "validation/target_universe_v1/historical_v1.json"
HEALTH = ROOT / "validation/target_universe_health_v1/full/results.json"
OUTPUT = ROOT / "config/source_registries/production_active_v1.json"
UNIVERSE_GIT_BLOB_SHA = "e3580590b8446cea8f1315169a862e5d332e53b1"
REGISTRY_ID = "ats-active-2026-09-v1"


def build(output: Path = OUTPUT) -> None:
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    health = json.loads(HEALTH.read_text(encoding="utf-8"))
    registry = build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha=UNIVERSE_GIT_BLOB_SHA,
        registry_id=REGISTRY_ID,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(registry.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
