from __future__ import annotations

import hashlib
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import job_scout.workday_hydration_benchmark as benchmark
from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    PostingFreshnessRule,
    RemoteStatus,
    RuleIntent,
    SearchBrief,
    UnknownEligibilityPolicy,
)
from job_scout.production_registry import ProductionSourceRegistry, sha256_json


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _registry() -> ProductionSourceRegistry:
    return ProductionSourceRegistry(
        registry_id="job37-test",
        target_universe_git_blob_sha="b" * 40,
        health_manifest_sha256="a" * 64,
        health_evidence_updated_at="2026-09-30T00:00:00+00:00",
        target_counts_by_source={"workday": 1},
        targets=[
            {
                "target_identity": "workday:alpha.wd1.myworkdayjobs.com:alpha:External",
                "source": "workday",
                "coordinates": {
                    "host": "alpha.wd1.myworkdayjobs.com",
                    "tenant": "alpha",
                    "site": "External",
                },
                "company_hint": "Alpha",
                "health_current_postings": 4,
                "health_inventory_exact": True,
            }
        ],
    )


def _job(
    source_id: str,
    *,
    url: str,
    posted_at: datetime,
    company: str = "Alpha",
) -> Job:
    return Job(
        id=source_id,
        source="workday",
        source_job_id=source_id,
        source_board_id="alpha.wd1.myworkdayjobs.com:alpha:External",
        title="Software Engineer",
        company=company,
        description_text="Build and maintain reliable software systems. " * 12,
        job_url=url,
        canonical_url=url,
        country="United States",
        eligible_countries={"United States"},
        remote_status=RemoteStatus.REMOTE,
        posted_at=posted_at,
        discovered_at=posted_at,
        last_seen_at=posted_at,
        content_fingerprint=f"fp-{source_id}",
    )


def _write_historical_ledger(path: Path, historical_job: Job) -> Path:
    identity_hash = benchmark._identity_digest(historical_job)
    binary = b"JSH2" + struct.pack(">II", 1, 0) + identity_hash
    binary_path = path / "historical.bin"
    binary_path.write_bytes(binary)
    metadata = {
        "ledger_version": "workday-historical-suppression-hashes-v1",
        "client_id": "client",
        "source_logical_rows": 1,
        "source_exact_supported_identity_rows": 1,
        "source_workday_exact_identity_rows": 1,
        "workday_unique_exact_identity_hashes": 1,
        "workday_unique_normalized_url_hashes": 0,
        "logical_corpus_sha256": "a" * 64,
        "binary_file": binary_path.name,
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "hash_algorithm": "sha256",
        "identity_hash_encoding": (
            "canonical-json [source, source_board_id, source_job_id]"
        ),
        "url_hash_encoding": "utf8 canonical_url",
    }
    metadata["ledger_sha256"] = sha256_json(metadata)
    metadata_path = path / "historical.json"
    _write_json(metadata_path, metadata)
    return metadata_path


def test_job37_full_accounting_uses_authoritative_identity_history_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    jobs = [
        _job(
            "HIST",
            url="https://example.test/jobs/historical",
            posted_at=now - timedelta(hours=2),
            company="History Co",
        ),
        _job("A", url="https://example.test/jobs/shared", posted_at=now - timedelta(hours=2)),
        _job("B", url="https://example.test/jobs/shared", posted_at=now - timedelta(hours=3)),
        _job(
            "STALE",
            url="https://example.test/jobs/stale",
            posted_at=now - timedelta(hours=30),
            company="Stale Co",
        ),
    ]

    class FakeCollector:
        client = None

        def __init__(self, *, detail_concurrency: int) -> None:
            assert detail_concurrency == 4

        def hydrate_paths(self, target, paths):
            assert len(paths) == 4
            return CollectionResult(
                source="workday",
                target=target,
                status=CollectionStatus.SUCCESS,
                jobs=jobs,
                raw_postings_received=4,
            )

    monkeypatch.setattr(benchmark, "WorkdayCollector", FakeCollector)

    registry = _registry()
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(registry.model_dump_json(), encoding="utf-8")

    target_identity = registry.targets[0].target_identity
    candidate_payload = {
        "manifest_version": "workday-hydration-candidates-v2",
        "source_index_run_id": 1,
        "brief_client_id": "client",
        "trusted_candidate_count": 4,
        "candidates": [
            {
                "target_identity": target_identity,
                "external_path": f"/job/Test/Role_{value}-list-alias",
                "source_job_id": f"{value}-list-alias",
                "historically_surfaced": False,
                "historical_match_basis": None,
            }
            for value in ("HIST", "A", "B", "STALE")
        ],
    }
    candidate_payload["manifest_sha256"] = sha256_json(candidate_payload)
    candidates_path = tmp_path / "candidates.json"
    _write_json(candidates_path, candidate_payload)

    historical_path = _write_historical_ledger(tmp_path, jobs[0])

    brief = SearchBrief(
        client_id="client",
        target_roles=["Software Engineer"],
        management_roles=RuleIntent.IGNORE,
        posting_freshness=PostingFreshnessRule(
            max_age_hours=24,
            unknown_policy=UnknownEligibilityPolicy.REJECT,
        ),
    )
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(brief.model_dump_json(), encoding="utf-8")

    report = benchmark.run_hydration_benchmark(
        registry_path=registry_path,
        candidates_path=candidates_path,
        historical_hashes_path=historical_path,
        brief_path=brief_path,
        database_path=tmp_path / "job37.sqlite3",
        expected_candidates=4,
    )

    assert report["candidate_paths_attempted"] == 4
    assert report["successful_detail_paths"] == 4
    assert report["hydration_failures"] == 0
    assert report["hydrated_unique_jobs"] == 4
    assert report["provider_identity_collapses"] == 0
    assert report["semantic_match_postings"] == 4
    assert report["semantic_match_groups"] == 3
    assert report["practical_duplicate_postings_collapsed"] == 1
    assert report["historically_suppressed_groups"] == 1
    assert report["freshness_group_suppressions"] == {"stale_posting": 1}
    assert report["fresh_unique_delivery_groups"] == 1
    assert report["final_delivery_policy_eligible_count"] == 1
    assert report["prior_delivery_suppressed_groups"] == 0


def test_job37_rejects_tampered_candidate_manifest(tmp_path: Path) -> None:
    payload = {
        "manifest_version": "workday-hydration-candidates-v2",
        "source_index_run_id": 1,
        "brief_client_id": "client",
        "trusted_candidate_count": 1,
        "candidates": [
            {
                "target_identity": "workday:alpha.wd1.myworkdayjobs.com:alpha:External",
                "external_path": "/job/Test/R1",
            }
        ],
    }
    payload["manifest_sha256"] = "0" * 64
    path = tmp_path / "candidates.json"
    _write_json(path, payload)

    with pytest.raises(ValueError, match="manifest_sha256 does not match"):
        benchmark._verify_self_digest(payload, "manifest_sha256")
