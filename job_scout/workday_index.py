"""Read-only Workday list index for cheap discovery before detail hydration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scout.domain.models import CollectionStatus, SearchBrief
from job_scout.normalization.core import normalize_title
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    ProductionTarget,
    build_shard_manifest,
    load_production_registry,
    sha256_json,
)
from job_scout.search_brief import load_search_brief

LIMIT = 20
CAP_TOTAL = 2000
RETRIES = 2
USER_AGENT = "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"
MAX_INDEX_TARGETS = 536
_POSTED_DAYS = re.compile(r"^posted\s+(\d+)\+?\s+days?\s+ago$", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkdayIndexPosting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_path: str
    title: str | None = None
    locations_text: str | None = None
    posted_on: str | None = None
    bullet_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_path(self) -> WorkdayIndexPosting:
        parsed = urlsplit(self.external_path)
        if (
            not self.external_path.startswith("/job/")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Workday externalPath")
        return self


class WorkdayIndexTargetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    status: CollectionStatus
    started_at: datetime
    completed_at: datetime
    runtime_ms: int = Field(ge=0)
    broad_total: int | None = Field(default=None, ge=0)
    provider_rows_seen: int = Field(ge=0)
    coverage_mode: Literal[
        "unknown",
        "broad",
        "job_family_group_partition",
        "capped_partial",
    ] = "unknown"
    postings: list[WorkdayIndexPosting] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> WorkdayIndexTargetResult:
        if self.completed_at < self.started_at:
            raise ValueError("target completion precedes start")
        if len({posting.external_path for posting in self.postings}) != len(self.postings):
            raise ValueError("target index contains duplicate externalPath values")
        if self.provider_rows_seen < len(self.postings):
            raise ValueError("provider row count cannot be lower than unique postings")
        if self.status is not CollectionStatus.SUCCESS and not self.errors:
            raise ValueError("non-success Workday index target requires an error")
        return self


class WorkdayIndexMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets_attempted: int = Field(ge=0)
    targets_succeeded: int = Field(ge=0)
    targets_partial: int = Field(ge=0)
    targets_failed: int = Field(ge=0)
    provider_rows_seen: int = Field(ge=0)
    unique_postings: int = Field(ge=0)
    postings_with_age_hint: int = Field(ge=0)
    postings_definitely_older_than_72h: int = Field(ge=0)
    postings_recent_or_uncertain_for_72h: int = Field(ge=0)
    target_runtime_p50_ms: int = Field(ge=0)
    target_runtime_p95_ms: int = Field(ge=0)


class WorkdayIndexArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version: Literal["workday-list-index-v1"] = "workday-list-index-v1"
    registry_id: str
    registry_sha256: str
    shard_manifest_sha256: str
    shard_id: str
    started_at: datetime
    completed_at: datetime
    metrics: WorkdayIndexMetrics
    targets: list[WorkdayIndexTargetResult]
    artifact_sha256: str

    @model_validator(mode="after")
    def validate_artifact(self) -> WorkdayIndexArtifact:
        if self.completed_at < self.started_at:
            raise ValueError("index artifact completion precedes start")
        if len({target.target_identity for target in self.targets}) != len(self.targets):
            raise ValueError("index artifact contains duplicate targets")
        expected = _metrics(self.targets)
        if self.metrics != expected:
            raise ValueError("Workday index metrics do not reconcile")
        payload = self.model_dump(mode="json", exclude={"artifact_sha256"})
        if self.artifact_sha256 != sha256_json(payload):
            raise ValueError("Workday index artifact hash is invalid")
        return self


class WorkdayIndexPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_version: Literal["workday-index-plan-v1"] = "workday-index-plan-v1"
    parent_registry_id: str
    parent_registry_sha256: str
    index_registry_id: str
    index_registry_sha256: str
    shard_manifest_sha256: str
    target_count: int = Field(ge=1, le=MAX_INDEX_TARGETS)
    shard_count: int = Field(ge=1)
    matrix: dict[str, list[dict[str, str]]]


class WorkdayIndexAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_version: Literal["workday-index-analysis-v2"] = "workday-index-analysis-v2"
    registry_id: str
    shard_manifest_sha256: str
    client_id: str
    coverage_complete: bool
    targets_scanned: int = Field(ge=0)
    targets_succeeded: int = Field(ge=0)
    targets_partial: int = Field(ge=0)
    targets_failed: int = Field(ge=0)
    targets_with_hydration_candidates: int = Field(ge=0)
    provider_rows_seen: int = Field(ge=0)
    unique_postings: int = Field(ge=0)
    postings_with_age_hint: int = Field(ge=0)
    definitely_older_than_72h: int = Field(ge=0)
    recent_or_uncertain_for_72h: int = Field(ge=0)
    role_title_candidates: int = Field(ge=0)
    recent_or_uncertain_role_title_candidates: int = Field(ge=0)
    hydration_candidates_on_complete_targets: int = Field(ge=0)
    hydration_candidates_on_incomplete_targets: int = Field(ge=0)
    incomplete_targets: list[dict[str, Any]] = Field(default_factory=list)
    top_targets: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> WorkdayIndexAnalysis:
        if self.targets_scanned != (
            self.targets_succeeded + self.targets_partial + self.targets_failed
        ):
            raise ValueError("Workday index analysis target counts do not reconcile")
        expected_complete = self.targets_partial == 0 and self.targets_failed == 0
        if self.coverage_complete is not expected_complete:
            raise ValueError("Workday index analysis coverage flag does not reconcile")
        if self.recent_or_uncertain_role_title_candidates != (
            self.hydration_candidates_on_complete_targets
            + self.hydration_candidates_on_incomplete_targets
        ):
            raise ValueError("Workday index analysis candidate counts do not reconcile")
        if len(self.incomplete_targets) != self.targets_partial + self.targets_failed:
            raise ValueError("Workday index analysis incomplete targets do not reconcile")
        if self.targets_with_hydration_candidates > self.targets_scanned:
            raise ValueError("hydration-candidate target count exceeds scanned targets")
        return self


class _Partition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    advertised_count: int = Field(ge=0)


class _QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    postings: list[WorkdayIndexPosting]
    total: int | None
    provider_rows_seen: int = Field(ge=0)
    errors: list[str]
    facets: list[dict[str, Any]]
    initial_status: CollectionStatus | None = None

    @property
    def complete(self) -> bool:
        return self.total is not None and not self.errors and len(self.postings) == self.total


def posted_on_age_days(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().split())
    if normalized.casefold() == "posted today":
        return 0
    if normalized.casefold() == "posted yesterday":
        return 1
    match = _POSTED_DAYS.fullmatch(normalized)
    return int(match.group(1)) if match else None


def definitely_older_than_72h(value: str | None) -> bool:
    age = posted_on_age_days(value)
    return age is not None and age >= 4


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _metrics(targets: list[WorkdayIndexTargetResult]) -> WorkdayIndexMetrics:
    postings = [posting for target in targets for posting in target.postings]
    succeeded = sum(target.status is CollectionStatus.SUCCESS for target in targets)
    partial = sum(target.status is CollectionStatus.PARTIAL for target in targets)
    failed = len(targets) - succeeded - partial
    stale = sum(definitely_older_than_72h(posting.posted_on) for posting in postings)
    hinted = sum(posted_on_age_days(posting.posted_on) is not None for posting in postings)
    runtimes = [target.runtime_ms for target in targets]
    return WorkdayIndexMetrics(
        targets_attempted=len(targets),
        targets_succeeded=succeeded,
        targets_partial=partial,
        targets_failed=failed,
        provider_rows_seen=sum(target.provider_rows_seen for target in targets),
        unique_postings=len(postings),
        postings_with_age_hint=hinted,
        postings_definitely_older_than_72h=stale,
        postings_recent_or_uncertain_for_72h=len(postings) - stale,
        target_runtime_p50_ms=_percentile(runtimes, 0.50),
        target_runtime_p95_ms=_percentile(runtimes, 0.95),
    )


def _listing(row: Any) -> WorkdayIndexPosting | None:
    if not isinstance(row, dict):
        return None
    path = row.get("externalPath")
    if not isinstance(path, str):
        return None
    title = row.get("title")
    locations = row.get("locationsText")
    posted = row.get("postedOn")
    bullets = row.get("bulletFields")
    return WorkdayIndexPosting(
        external_path=path,
        title=title if isinstance(title, str) and title.strip() else None,
        locations_text=locations if isinstance(locations, str) and locations.strip() else None,
        posted_on=posted if isinstance(posted, str) and posted.strip() else None,
        bullet_fields=(
            [value for value in bullets if isinstance(value, str)]
            if isinstance(bullets, list)
            else []
        ),
    )


class WorkdayIndexScanner:
    """Index public Workday list rows without requesting per-job detail payloads."""

    def __init__(self, client: httpx.Client | None = None, *, delay: float = 0.5) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={
                "Accept": "application/json",
                "Accept-Language": "en-US",
                "User-Agent": USER_AGENT,
            },
            transport=httpx.HTTPTransport(retries=2),
        )
        self.delay = delay

    @staticmethod
    def _base(target: ProductionTarget) -> str:
        coordinates = target.coordinates
        return (
            f"https://{coordinates['host']}/wday/cxs/"
            f"{quote(coordinates['tenant'], safe='')}/{quote(coordinates['site'], safe='')}"
        )

    def close(self) -> None:
        self.client.close()

    def _pause(self, multiplier: int = 1) -> None:
        if self.delay > 0:
            time.sleep(self.delay * multiplier)

    def _request(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> tuple[httpx.Response | None, CollectionStatus | None, str | None]:
        for attempt in range(RETRIES + 1):
            try:
                response = self.client.post(url, json=payload)
            except httpx.RequestError as exc:
                status = CollectionStatus.NETWORK_FAILURE
                error = f"network {type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response, None, None
                if response.status_code == 404:
                    return None, CollectionStatus.INVALID_TARGET, "HTTP 404"
                if response.status_code == 401:
                    return None, CollectionStatus.AUTHENTICATION_FAILURE, "HTTP 401"
                if response.status_code == 403:
                    return None, CollectionStatus.FORBIDDEN, "HTTP 403"
                if response.status_code == 429:
                    status = CollectionStatus.RATE_LIMITED
                    error = "HTTP 429"
                elif response.status_code >= 500:
                    status = CollectionStatus.PROVIDER_ERROR
                    error = f"HTTP {response.status_code}"
                else:
                    return None, CollectionStatus.PROVIDER_ERROR, f"HTTP {response.status_code}"
            if attempt < RETRIES:
                self._pause(attempt + 1)
                continue
            return None, status, error
        raise AssertionError("unreachable")

    def _query(
        self,
        target: ProductionTarget,
        facets: dict[str, list[str]],
        *,
        scope: str,
    ) -> _QueryResult:
        url = f"{self._base(target)}/jobs"
        postings: list[WorkdayIndexPosting] = []
        signatures: set[str] = set()
        first_total: int | None = None
        result_facets: list[dict[str, Any]] = []
        errors: list[str] = []
        initial_status: CollectionStatus | None = None
        provider_rows_seen = 0
        offset = 0

        while first_total is None or offset < first_total:
            response, status, request_error = self._request(
                url,
                {
                    "appliedFacets": facets,
                    "limit": LIMIT,
                    "offset": offset,
                    "searchText": "",
                },
            )
            if response is None:
                initial_status = status if offset == 0 else None
                errors.append(f"{scope} offset {offset}: {request_error}")
                break
            try:
                body = response.json()
            except ValueError:
                errors.append(f"{scope} offset {offset}: response is not JSON")
                if offset == 0:
                    initial_status = CollectionStatus.PARSE_FAILURE
                break
            if not isinstance(body, dict):
                errors.append(f"{scope} offset {offset}: response is not an object")
                if offset == 0:
                    initial_status = CollectionStatus.PARSE_FAILURE
                break
            if first_total is None:
                total = body.get("total")
                if not isinstance(total, int) or total < 0:
                    errors.append(f"{scope} offset 0: response has no non-negative integer total")
                    initial_status = CollectionStatus.PARSE_FAILURE
                    break
                first_total = total
                raw_facets = body.get("facets") or []
                result_facets = raw_facets if isinstance(raw_facets, list) else []
            rows = body.get("jobPostings") or []
            if not isinstance(rows, list):
                errors.append(f"{scope} offset {offset}: jobPostings is not a list")
                break
            provider_rows_seen += len(rows)
            signature = hashlib.sha256(
                json.dumps(
                    rows,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                ).encode()
            ).hexdigest()
            if signature in signatures:
                errors.append(f"{scope} offset {offset}: repeated page signature")
                break
            signatures.add(signature)

            page: list[WorkdayIndexPosting] = []
            invalid_rows = 0
            for row in rows:
                try:
                    item = _listing(row)
                except ValueError:
                    item = None
                if item is None:
                    invalid_rows += 1
                    continue
                page.append(item)
            if invalid_rows:
                errors.append(
                    f"{scope} offset {offset}: {invalid_rows} rows have invalid externalPath"
                )
            postings.extend(page)
            if not rows and offset < first_total:
                errors.append(f"{scope} offset {offset}: empty page before declared total")
                break
            offset += LIMIT
            self._pause()

        paths = [posting.external_path for posting in postings]
        if first_total is not None and len(postings) != first_total:
            errors.append(
                f"{scope}: retrieved {len(postings)} valid rows but first page declared "
                f"{first_total}"
            )
        if len(set(paths)) != len(paths):
            errors.append(f"{scope}: duplicate externalPath values across pages")
        return _QueryResult(
            postings=postings,
            total=first_total,
            provider_rows_seen=provider_rows_seen,
            errors=errors,
            facets=result_facets,
            initial_status=initial_status,
        )

    @staticmethod
    def _partitions(facets: list[dict[str, Any]]) -> tuple[list[_Partition] | None, str | None]:
        facet = next(
            (
                item
                for item in facets
                if isinstance(item, dict) and item.get("facetParameter") == "jobFamilyGroup"
            ),
            None,
        )
        values = facet.get("values") if isinstance(facet, dict) else None
        if not isinstance(values, list) or len(values) < 2:
            return None, "broad result is capped and has no safe jobFamilyGroup partition contract"
        partitions: list[_Partition] = []
        for value in values:
            if not isinstance(value, dict):
                return None, "jobFamilyGroup partition metadata is malformed"
            identifier = value.get("id")
            count = value.get("count")
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(count, int)
                or count < 0
                or count >= CAP_TOTAL
            ):
                return None, "jobFamilyGroup partition metadata is unsafe for cap recovery"
            partitions.append(_Partition(identifier=identifier, advertised_count=count))
        if len({item.identifier for item in partitions}) != len(partitions):
            return None, "jobFamilyGroup partition identifiers are not unique"
        if sum(item.advertised_count for item in partitions) <= CAP_TOTAL:
            return None, "jobFamilyGroup partition counts do not establish coverage beyond the cap"
        return partitions, None

    @staticmethod
    def _merge(
        destination: dict[str, WorkdayIndexPosting],
        postings: list[WorkdayIndexPosting],
        errors: list[str],
    ) -> None:
        for posting in postings:
            existing = destination.get(posting.external_path)
            if existing is None:
                destination[posting.external_path] = posting
                continue
            if existing != posting:
                errors.append(
                    f"conflicting list metadata for externalPath {posting.external_path}"
                )

    def scan(self, target: ProductionTarget) -> WorkdayIndexTargetResult:
        if target.source != "workday":
            raise ValueError("Workday index scanner requires a Workday production target")
        started_at = utc_now()
        started_clock = time.perf_counter()
        broad = self._query(target, {}, scope="broad")
        if broad.total is None:
            completed_at = utc_now()
            return WorkdayIndexTargetResult(
                target_identity=target.target_identity,
                status=broad.initial_status or CollectionStatus.PARSE_FAILURE,
                started_at=started_at,
                completed_at=completed_at,
                runtime_ms=max(0, round((time.perf_counter() - started_clock) * 1000)),
                broad_total=None,
                provider_rows_seen=broad.provider_rows_seen,
                coverage_mode="unknown",
                postings=[],
                errors=broad.errors or ["missing broad total"],
            )

        errors = list(broad.errors)
        provider_rows_seen = broad.provider_rows_seen
        merged: dict[str, WorkdayIndexPosting] = {}
        self._merge(merged, broad.postings, errors)
        coverage_mode: Literal[
            "unknown", "broad", "job_family_group_partition", "capped_partial"
        ]
        coverage_mode = "broad"

        if broad.total == CAP_TOTAL:
            partitions, partition_error = self._partitions(broad.facets)
            if partition_error:
                errors.append(partition_error)
                coverage_mode = "capped_partial"
            else:
                assert partitions is not None
                partition_complete = True
                seen_partition_paths: set[str] = set()
                for partition in partitions:
                    result = self._query(
                        target,
                        {"jobFamilyGroup": [partition.identifier]},
                        scope=f"jobFamilyGroup:{partition.identifier}",
                    )
                    provider_rows_seen += result.provider_rows_seen
                    partition_paths = {
                        posting.external_path for posting in result.postings
                    }
                    overlap = seen_partition_paths & partition_paths
                    if overlap:
                        partition_complete = False
                        errors.append(
                            "jobFamilyGroup partitions overlap by externalPath: "
                            f"{len(overlap)} duplicate path(s)"
                        )
                    seen_partition_paths.update(partition_paths)
                    self._merge(merged, result.postings, errors)
                    errors.extend(result.errors)
                    if result.total != partition.advertised_count or not result.complete:
                        partition_complete = False
                        errors.append(
                            "jobFamilyGroup partition "
                            f"{partition.identifier} did not reconcile with advertised count "
                            f"{partition.advertised_count}"
                        )
                coverage_mode = (
                    "job_family_group_partition"
                    if partition_complete and not errors
                    else "capped_partial"
                )
        elif broad.total > CAP_TOTAL:
            # A historical 2,000-row cap must not override stronger live evidence.
            # If this exact query enumerated every declared row with unique paths and
            # no errors, broad pagination itself proves coverage beyond the old cap.
            if broad.complete:
                coverage_mode = "broad"
            else:
                errors.append(
                    f"broad total {broad.total} exceeds {CAP_TOTAL} and exact coverage "
                    "was not proven"
                )
                coverage_mode = "capped_partial"

        completed_at = utc_now()
        postings = [merged[path] for path in sorted(merged)]
        return WorkdayIndexTargetResult(
            target_identity=target.target_identity,
            status=CollectionStatus.PARTIAL if errors else CollectionStatus.SUCCESS,
            started_at=started_at,
            completed_at=completed_at,
            runtime_ms=max(0, round((time.perf_counter() - started_clock) * 1000)),
            broad_total=broad.total,
            provider_rows_seen=provider_rows_seen,
            coverage_mode=coverage_mode,
            postings=postings,
            errors=errors,
        )


def _subset_registry(
    registry: ProductionSourceRegistry,
    *,
    target_count: int,
) -> ProductionSourceRegistry:
    candidates = [target for target in registry.targets if target.source == "workday"]
    if not 1 <= target_count <= min(MAX_INDEX_TARGETS, len(candidates)):
        raise ValueError("Workday index target count is outside the production registry")
    ordered = sorted(
        candidates,
        key=lambda target: (
            hashlib.sha256(target.target_identity.encode()).hexdigest(),
            target.target_identity,
        ),
    )
    selected = sorted(ordered[:target_count], key=lambda target: target.target_identity)
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "target_identities": [target.target_identity for target in selected],
        }
    )[:16]
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-workday-index-{identity}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source={"workday": len(selected)},
        targets=selected,
    )


def build_index_plan(
    registry: ProductionSourceRegistry,
    *,
    target_count: int,
    shard_count: int,
) -> tuple[WorkdayIndexPlan, ProductionSourceRegistry, CollectionShardManifest]:
    subset = _subset_registry(registry, target_count=target_count)
    if not 1 <= shard_count <= target_count:
        raise ValueError("Workday index shard count must be from 1 to target count")
    manifest = build_shard_manifest(
        subset,
        shard_counts_by_source={"workday": shard_count},
    )
    plan = WorkdayIndexPlan(
        parent_registry_id=registry.registry_id,
        parent_registry_sha256=sha256_json(registry.model_dump(mode="json")),
        index_registry_id=subset.registry_id,
        index_registry_sha256=sha256_json(subset.model_dump(mode="json")),
        shard_manifest_sha256=manifest.manifest_sha256,
        target_count=target_count,
        shard_count=shard_count,
        matrix={
            "include": [
                {"shard_id": shard.shard_id, "source": shard.source}
                for shard in manifest.shards
            ]
        },
    )
    return plan, subset, manifest


def scan_index_shard(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    shard_id: str,
    scanner_factory: Callable[[], WorkdayIndexScanner] = WorkdayIndexScanner,
) -> WorkdayIndexArtifact:
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    if manifest.registry_id != registry.registry_id or manifest.registry_sha256 != registry_sha:
        raise ValueError("index shard manifest does not match registry")
    shards = [shard for shard in manifest.shards if shard.shard_id == shard_id]
    if len(shards) != 1 or shards[0].source != "workday":
        raise ValueError("requested Workday index shard is invalid")
    shard = shards[0]
    targets = {target.target_identity: target for target in registry.targets}
    started_at = utc_now()
    results: list[WorkdayIndexTargetResult] = []
    for identity in shard.target_identities:
        target = targets.get(identity)
        if target is None or target.source != "workday":
            raise ValueError("index shard contains target outside Workday registry subset")
        scanner = scanner_factory()
        try:
            results.append(scanner.scan(target))
        finally:
            scanner.close()
    completed_at = utc_now()
    payload = {
        "artifact_version": "workday-list-index-v1",
        "registry_id": registry.registry_id,
        "registry_sha256": registry_sha,
        "shard_manifest_sha256": manifest.manifest_sha256,
        "shard_id": shard_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "metrics": _metrics(results),
        "targets": results,
    }
    serialized = {
        "artifact_version": "workday-list-index-v1",
        "registry_id": registry.registry_id,
        "registry_sha256": registry_sha,
        "shard_manifest_sha256": manifest.manifest_sha256,
        "shard_id": shard_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "metrics": payload["metrics"].model_dump(mode="json"),
        "targets": [result.model_dump(mode="json") for result in results],
    }
    artifact = WorkdayIndexArtifact(
        **payload,
        artifact_sha256=sha256_json(serialized),
    )
    expected = shard.target_identities
    actual = [target.target_identity for target in artifact.targets]
    if actual != expected:
        raise ValueError("Workday index artifact target coverage/order does not match manifest")
    return artifact


def validate_complete_index_set(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    artifacts: list[WorkdayIndexArtifact],
) -> None:
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    if manifest.registry_id != registry.registry_id or manifest.registry_sha256 != registry_sha:
        raise ValueError("index manifest does not match registry")
    verified = [
        WorkdayIndexArtifact.model_validate(artifact.model_dump(mode="json"))
        for artifact in artifacts
    ]
    by_id = {artifact.shard_id: artifact for artifact in verified}
    if len(by_id) != len(verified):
        raise ValueError("duplicate Workday index shard artifact")
    expected = {shard.shard_id for shard in manifest.shards}
    if set(by_id) != expected:
        raise ValueError("Workday index artifact set is incomplete or contains unknown shards")
    for shard in manifest.shards:
        artifact = by_id[shard.shard_id]
        if (
            artifact.registry_id != registry.registry_id
            or artifact.registry_sha256 != registry_sha
            or artifact.shard_manifest_sha256 != manifest.manifest_sha256
            or [target.target_identity for target in artifact.targets] != shard.target_identities
        ):
            raise ValueError(f"Workday index provenance mismatch: {shard.shard_id}")


def role_title_candidate(title: str | None, brief: SearchBrief) -> bool:
    """Return whether list-title evidence can still satisfy the matcher role gate."""
    if not title:
        return False
    normalized = normalize_title(title)
    if not any(normalize_title(role) in normalized for role in brief.target_roles):
        return False
    return not any(normalize_title(role) in normalized for role in brief.excluded_titles)


def analyze_index(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    artifacts: list[WorkdayIndexArtifact],
    brief: SearchBrief,
) -> WorkdayIndexAnalysis:
    validate_complete_index_set(
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
    )
    target_results = [
        target for artifact in artifacts for target in artifact.targets
    ]
    target_status = {target.target_identity: target.status for target in target_results}
    postings = [
        (target.target_identity, posting)
        for target in target_results
        for posting in target.postings
    ]
    candidates_by_target: Counter[str] = Counter()
    role_candidates = 0
    recent_role_candidates = 0
    complete_target_candidates = 0
    incomplete_target_candidates = 0
    hinted = 0
    stale = 0
    for target_identity, posting in postings:
        age = posted_on_age_days(posting.posted_on)
        hinted += age is not None
        is_stale = definitely_older_than_72h(posting.posted_on)
        stale += is_stale
        if role_title_candidate(posting.title, brief):
            role_candidates += 1
            if not is_stale:
                recent_role_candidates += 1
                candidates_by_target[target_identity] += 1
                if target_status[target_identity] is CollectionStatus.SUCCESS:
                    complete_target_candidates += 1
                else:
                    incomplete_target_candidates += 1

    status_counts = Counter(target.status for target in target_results)
    incomplete_targets = [
        {
            "target_identity": target.target_identity,
            "status": target.status.value,
            "broad_total": target.broad_total,
            "coverage_mode": target.coverage_mode,
            "valid_postings_indexed": len(target.postings),
            "provider_rows_seen": target.provider_rows_seen,
            "errors": target.errors,
        }
        for target in target_results
        if target.status is not CollectionStatus.SUCCESS
    ]
    top_targets = [
        {
            "target_identity": target,
            "target_status": target_status[target].value,
            "hydration_candidates": count,
        }
        for target, count in candidates_by_target.most_common(25)
    ]
    partial = status_counts[CollectionStatus.PARTIAL]
    succeeded = status_counts[CollectionStatus.SUCCESS]
    failed = len(target_results) - succeeded - partial
    return WorkdayIndexAnalysis(
        registry_id=registry.registry_id,
        shard_manifest_sha256=manifest.manifest_sha256,
        client_id=brief.client_id,
        coverage_complete=partial == 0 and failed == 0,
        targets_scanned=len(target_results),
        targets_succeeded=succeeded,
        targets_partial=partial,
        targets_failed=failed,
        targets_with_hydration_candidates=len(candidates_by_target),
        provider_rows_seen=sum(
            artifact.metrics.provider_rows_seen for artifact in artifacts
        ),
        unique_postings=len(postings),
        postings_with_age_hint=hinted,
        definitely_older_than_72h=stale,
        recent_or_uncertain_for_72h=len(postings) - stale,
        role_title_candidates=role_candidates,
        recent_or_uncertain_role_title_candidates=recent_role_candidates,
        hydration_candidates_on_complete_targets=complete_target_candidates,
        hydration_candidates_on_incomplete_targets=incomplete_target_candidates,
        incomplete_targets=incomplete_targets,
        top_targets=top_targets,
    )


def _write_json(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.workday_index")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--registry", required=True, type=Path)
    plan.add_argument("--target-count", required=True, type=int)
    plan.add_argument("--shard-count", required=True, type=int)
    plan.add_argument("--output-dir", required=True, type=Path)

    scan = commands.add_parser("scan")
    scan.add_argument("--registry", required=True, type=Path)
    scan.add_argument("--manifest", required=True, type=Path)
    scan.add_argument("--shard-id", required=True)
    scan.add_argument("--output", required=True, type=Path)

    analyze = commands.add_parser("analyze")
    analyze.add_argument("--registry", required=True, type=Path)
    analyze.add_argument("--manifest", required=True, type=Path)
    analyze.add_argument("--artifacts-dir", required=True, type=Path)
    analyze.add_argument("--brief", required=True, type=Path)
    analyze.add_argument("--output", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            parent = load_production_registry(args.registry)
            index_plan, subset, manifest = build_index_plan(
                parent,
                target_count=args.target_count,
                shard_count=args.shard_count,
            )
            _write_json(args.output_dir / "plan.json", index_plan)
            _write_json(args.output_dir / "registry.json", subset)
            _write_json(args.output_dir / "manifest.json", manifest)
            _write_json(args.output_dir / "matrix.json", index_plan.matrix)
            print(json.dumps(index_plan.model_dump(mode="json"), sort_keys=True))
            return

        registry = load_production_registry(args.registry)
        manifest = CollectionShardManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        if args.command == "scan":
            artifact = scan_index_shard(
                registry=registry,
                manifest=manifest,
                shard_id=args.shard_id,
            )
            _write_json(args.output, artifact)
            print(json.dumps(artifact.model_dump(mode="json"), sort_keys=True))
            return

        artifact_paths = sorted(args.artifacts_dir.glob("*.json"))
        if not artifact_paths:
            raise ValueError("no Workday index artifacts found")
        artifacts = [
            WorkdayIndexArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in artifact_paths
        ]
        report = analyze_index(
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            brief=load_search_brief(args.brief),
        )
        _write_json(args.output, report)
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"Workday index failed: {exc}")


if __name__ == "__main__":
    main()
