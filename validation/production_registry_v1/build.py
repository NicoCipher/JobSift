"""Rebuild the production-approved ATS registry from committed validation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from job_scout.production_registry import build_production_registry

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "validation/target_universe_v1/historical_v1.json"
HEALTH = ROOT / "validation/target_universe_health_v1/full/results.json"
OUTPUT = ROOT / "config/source_registries/production_active_v1.json"
MANIFEST = ROOT / "validation/target_universe_health_v1/full/manifest.json"
CHECKPOINTS = ROOT / "validation/target_universe_health_v1/full/checkpoints"
UNIVERSE_GIT_BLOB_SHA = "e3580590b8446cea8f1315169a862e5d332e53b1"
REGISTRY_ID = "ats-active-2026-09-v1"


def _checkpoint_path(checkpoints_path: Path, target_identity: str) -> Path:
    digest = hashlib.sha256(target_identity.encode()).hexdigest()
    return checkpoints_path / f"{digest}.json"


def _verify_health_results(
    health: dict,
    manifest: dict,
    *,
    checkpoints_path: Path,
) -> None:
    """Bind aggregate health results to independently persisted per-target evidence."""
    results = health.get("results")
    targets = manifest.get("targets")
    if not isinstance(results, list) or not isinstance(targets, list):
        raise TypeError("health evidence is missing results or manifest targets")
    if len(results) != len(targets):
        raise ValueError("health evidence is incomplete for the selected manifest")

    by_identity: dict[str, dict] = {}
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("health evidence contains a malformed result")
        identity = result.get("target_identity")
        if not isinstance(identity, str) or not identity:
            raise ValueError("health evidence contains a result without target identity")
        if identity in by_identity:
            raise ValueError("health evidence contains duplicate target results")
        by_identity[identity] = result

    expected_identities = {target["target_identity"] for target in targets}
    if set(by_identity) != expected_identities:
        raise ValueError("health evidence does not cover the exact manifest targets")

    for target in targets:
        identity = target["target_identity"]
        result = by_identity[identity]
        if (
            result.get("source") != target["source"]
            or result.get("manifest_sha256") != manifest["manifest_sha256"]
        ):
            raise ValueError("health result is not bound to its manifest target")
        checkpoint = _checkpoint_path(checkpoints_path, identity)
        try:
            checkpoint_result = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"missing or corrupt health checkpoint for {identity}") from exc
        if checkpoint_result != result:
            raise ValueError(f"health result does not match checkpoint for {identity}")


def build(
    output: Path = OUTPUT,
    *,
    universe_path: Path = UNIVERSE,
    health_path: Path = HEALTH,
    manifest_path: Path | None = None,
    registry_id: str = REGISTRY_ID,
) -> None:
    universe_bytes = universe_path.read_bytes()
    universe = json.loads(universe_bytes)
    health = json.loads(health_path.read_text(encoding="utf-8"))
    blob_sha = hashlib.sha1(
        b"blob " + str(len(universe_bytes)).encode() + b"\0" + universe_bytes
    ).hexdigest()

    selected_manifest = manifest_path or MANIFEST
    canonical_universe = universe_path.resolve() == UNIVERSE.resolve()
    canonical_manifest = selected_manifest.resolve() == MANIFEST.resolve()
    canonical_output = output.resolve() == OUTPUT.resolve()
    if not canonical_universe and (canonical_output or manifest_path is None):
        raise ValueError(
            "custom universe requires separate output and matching health manifest"
        )
    if canonical_output and (
        not canonical_universe
        or health_path.resolve() != HEALTH.resolve()
        or not canonical_manifest
    ):
        raise ValueError("production registry output requires canonical committed evidence")

    from validation.target_universe_health_v1.run import _verify_manifest

    manifest = _verify_manifest(
        json.loads(selected_manifest.read_text()),
        full=True,
        universe_path=universe_path,
    )
    if health.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("health evidence does not match universe manifest")

    checkpoints_path = (
        CHECKPOINTS
        if canonical_universe and canonical_manifest
        else health_path.parent / "checkpoints"
    )
    _verify_health_results(
        health,
        manifest,
        checkpoints_path=checkpoints_path,
    )
    registry = build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha=blob_sha,
        registry_id=registry_id,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            registry.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--universe", type=Path, default=UNIVERSE)
    parser.add_argument("--health", type=Path, default=HEALTH)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--registry-id", default=REGISTRY_ID)
    args = parser.parse_args()
    build(args.output, universe_path=args.universe, health_path=args.health, manifest_path=args.manifest, registry_id=args.registry_id)


if __name__ == "__main__":
    main()
