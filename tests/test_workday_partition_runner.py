from __future__ import annotations

import json
from pathlib import Path

import pytest

from validation.workday_contract_v1.nvidia_partition_union.run import (
    ManifestError,
    Runner,
    sha_json,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "fake error"

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(
        self,
        pages: dict[str, list[str]],
        *,
        fail_once: tuple[str, int] | None = None,
        repeat: bool = False,
    ) -> None:
        self.pages = pages
        self.fail_once = fail_once
        self.repeat = repeat
        self.post_calls: list[tuple[str, int]] = []
        self.get_calls: list[str] = []

    def post(self, url: str, *, json: dict) -> FakeResponse:
        facets = json["appliedFacets"]
        partition = facets.get("jobFamilyGroup", ["broad"])[0]
        offset = json["offset"]
        self.post_calls.append((partition, offset))
        if self.fail_once == (partition, offset):
            self.fail_once = None
            return FakeResponse(500)
        paths = (
            self.pages[partition]
            if partition != "broad"
            else [path for partition_paths in self.pages.values() for path in partition_paths]
        )
        rows = paths[offset : offset + 20]
        if self.repeat and partition == "id-0" and offset == 20:
            rows = paths[:20]
        return FakeResponse(
            200, {"total": len(paths), "jobPostings": [{"externalPath": path} for path in rows]}
        )

    def get(self, url: str) -> FakeResponse:
        self.get_calls.append(url)
        path = url.rsplit("/", 1)[-1]
        job_req_id = "REQ-SHARED" if path in {"id-0-0", "id-1-0"} else f"REQ-{path}"
        return FakeResponse(200, {"jobPostingInfo": {"jobReqId": job_req_id}})


def make_runner(tmp_path: Path, client: FakeClient) -> Runner:
    root = tmp_path / "contract"
    root.mkdir(exist_ok=True)
    partitions = [
        {"id": f"id-{index}", "label": f"Group {index}", "advertised_count": 1}
        for index in range(15)
    ]
    manifest = {
        "validation": "workday-nvidia-partition-union-v1",
        "host": "nvidia.wd5.myworkdayjobs.com",
        "tenant": "nvidia",
        "site": "NVIDIAExternalCareerSite",
        "broad_total": 2000,
        "partitions": partitions,
    }
    manifest["sha256"] = sha_json(manifest)
    (root / "freeze.json").write_text(json.dumps(manifest))
    return Runner(root, delay=0, retries=0, client=client, expected_manifest_sha=manifest["sha256"])


def pages(*, first_count: int = 1) -> dict[str, list[str]]:
    result = {f"id-{index}": [f"/job/id-{index}-0"] for index in range(15)}
    result["id-0"] = [f"/job/id-0-{index}" for index in range(first_count)]
    return result


def test_resume_skips_completed_pages_and_details(tmp_path: Path) -> None:
    first = FakeClient(pages(first_count=21), fail_once=("id-0", 20))
    runner = make_runner(tmp_path, first)
    status = runner.run()
    assert status["partitions_completed"] == 14
    assert (tmp_path / "contract" / "run" / "errors.jsonl").exists()

    resumed = FakeClient(pages(first_count=21))
    status = make_runner(tmp_path, resumed).run()
    assert status["partitions_completed"] == 15
    assert ("id-0", 0) not in resumed.post_calls
    assert resumed.post_calls == [("id-0", 20)]
    assert all(call.endswith("id-0-20") for call in resumed.get_calls)


def test_completed_work_is_not_fetched_again(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeClient(pages()))
    assert runner.run()["final_union_ready"] is True
    resumed = FakeClient(pages())
    assert make_runner(tmp_path, resumed).run()["final_union_ready"] is True
    assert resumed.post_calls == []
    assert resumed.get_calls == []


def test_repeated_page_is_recorded_and_remains_incomplete(tmp_path: Path) -> None:
    status = make_runner(tmp_path, FakeClient(pages(first_count=21), repeat=True)).run()
    assert status["partitions_completed"] == 14
    errors = (tmp_path / "contract" / "run" / "errors.jsonl").read_text()
    assert "repeated page signature" in errors


def test_final_union_deduplicates_provider_identity(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeClient(pages()))
    runner.run()
    summary = json.loads((tmp_path / "contract" / "run" / "union_summary.json").read_text())
    assert summary["unique_external_paths"] == 15
    assert summary["unique_job_req_ids"] == 14
    assert summary["duplicate_job_req_ids_across_partitions"] == 1
    assert summary["broad_query"]["paths_absent_from_partition_union"] == 0


def test_manifest_mismatch_refuses_to_run(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeClient(pages()))
    manifest_path = runner.root / "freeze.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["host"] = "other.wd5.myworkdayjobs.com"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ManifestError, match="freeze manifest hash"):
        Runner(runner.root, delay=0, client=FakeClient(pages()))
