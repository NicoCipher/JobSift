"""Replay frozen acquisitions read-only; never instantiate a collector or V1 repository.

Run with python -m validation.taiwo_sourcing_capacity_v2.run. Existing outputs
are verified byte-for-byte, never overwritten. Database inputs must be quiescent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from job_scout.dedupe.resolver import DEDUPE_VERSION, representative_key
from job_scout.domain.models import Job
from job_scout.matching.matcher import MATCHER_VERSION, match_job
from job_scout.search_brief import load_search_brief

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent
V1_ROOT = ROOT / "validation/taiwo_sourcing_capacity_v1"
A_DB = V1_ROOT / "stage_a/runtime/replay.sqlite3"
B_DB = V1_ROOT / "stage_b/runtime/replay.sqlite3"
V1 = ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
V2 = ROOT / "config/search_briefs/taiwo_operator_sourcing_v2.json"
V1_SHA = "7502c14a0dfc8ecdfaca9b2d1ba70a2c02aaec67aaf28bb495a5a7ba76d361f6"
B_SHA = "054238a3b5e61db43688416b84fcaee01b2dfe685a5e710db07fe61ccbf9396c"
A_SHA = "9358f118e1b30f0769c1aca8fc75560e27399fffe50547159feb8f6d25770211"
CLIENT = "taiwo_operator_sourcing_v1"
DESTINATION = "taiwo-sourcing-capacity-v1:stage-a-plus-stage-b"
REVISION = "taiwo-operator-sourcing-v2-conservative"
ADDITIONS = [
    "Technical Support Specialist",
    "IT Support",
    "IT Field Support Specialist",
    "Technical Support Analyst",
    "IT Systems Administrator",
    "Desktop Support Specialist",
    "Analyst Service Desk",
]
ELIGIBLE = {"possible_match", "strong_match"}
TITLE_REJECT = "title does not match a configured target role"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha_value(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def briefs():
    require(file_sha(V1) == V1_SHA, "V1 brief changed")
    old, new = load_search_brief(V1), load_search_brief(V2)
    require(new.target_roles == old.target_roles + ADDITIONS, "V2 vocabulary differs")
    require(
        old.model_dump(exclude={"target_roles"}) == new.model_dump(exclude={"target_roles"}),
        "non-title semantics or client identity changed",
    )
    require(new.client_id == CLIENT, "operator identity changed")
    require(MATCHER_VERSION == "deterministic-v5", "matcher version changed")
    require(DEDUPE_VERSION == "dedupe-v1", "dedupe version changed")
    return old, new


@contextmanager
def readonly(path):
    # Refuse live WAL/journal inputs instead of treating incomplete main-file bytes as frozen.
    for suffix in ("-wal", "-shm", "-journal"):
        require(not Path(str(path) + suffix).exists(), f"database not quiescent: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def input_fingerprints():
    paths = [V1, V2, A_DB, B_DB, Path(__file__)]
    # Stage A's summary supplies the inherited 44-group accounting reported below.
    # Stage B's summary is neither read nor used in this calculation; the Stage B
    # manifest and acquisition database are the required evidence instead.
    paths += [
        V1_ROOT / "stage_a" / "manifest.json",
        V1_ROOT / "stage_a" / "summary.json",
        V1_ROOT / "stage_b" / "manifest.json",
    ]
    paths += sorted((ROOT / "job_scout").rglob("*.py"))
    return {str(p.relative_to(ROOT)): file_sha(p) for p in paths}


def verify_input_fingerprints(expected):
    require(input_fingerprints() == expected, "frozen input fingerprints changed")


def frozen_v1_manifest(stage, expected):
    data = json.loads((V1_ROOT / stage / "manifest.json").read_text())
    claimed = data.pop("manifest_sha256")
    require(claimed == expected == sha_value(data), f"{stage} manifest mismatch")
    data["manifest_sha256"] = claimed
    return data


def delivery_projection(jobs, connection, client=CLIENT, destination=DESTINATION):
    """Production selection ordering/history/group semantics using only SELECTs."""
    counts = Counter()
    selected = {}
    details = []
    for job in sorted(jobs, key=representative_key):
        group = connection.execute(
            "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
        ).fetchone()
        require(group is not None, f"missing delivery group for {job.id}")
        group = group[0]
        identity = connection.execute(
            "SELECT 1 FROM historical_job_links WHERE client_id=? AND source=? "
            "AND source_board_id=? AND source_job_id=? LIMIT 1",
            (client, job.source, job.source_board_id, job.source_job_id),
        ).fetchone()
        url = (
            None
            if identity
            else connection.execute(
                "SELECT 1 FROM historical_job_links WHERE client_id=? AND normalized_url=? LIMIT 1",
                (client, str(job.canonical_url)),
            ).fetchone()
        )
        delivered = connection.execute(
            "SELECT 1 FROM group_deliveries WHERE group_id=? AND client_id=? AND destination=?",
            (group, client, destination),
        ).fetchone()
        if identity:
            outcome = "historical_identity"
        elif url:
            outcome = "historical_url"
        elif delivered:
            outcome = "already_delivered_postings"
        elif group in selected:
            outcome = "practical_duplicate_postings_collapsed"
        else:
            outcome = "fresh_groups"
            selected[group] = job.id
        counts[outcome] += 1
        details.append({"job_id": job.id, "group_id": group, "outcome": outcome})
    return {
        "eligible_postings": len(jobs),
        **{
            key: counts[key]
            for key in (
                "historical_identity",
                "historical_url",
                "already_delivered_postings",
                "practical_duplicate_postings_collapsed",
                "fresh_groups",
            )
        },
        "selected_groups": dict(sorted(selected.items())),
        "posting_evidence": sorted(details, key=lambda row: row["job_id"]),
    }


def handoff_targets(targets, rows):
    """Success is acquisition completion; a failed terminal V1 attempt is not acquisition."""
    by_id = {row["target_identity"]: row for row in rows}
    require(len(by_id) == len(rows), "duplicate target states")
    require(set(by_id) <= {t["target_identity"] for t in targets}, "unknown target state")
    result = []
    for target in targets:
        row = by_id.get(target["target_identity"])
        if row and row["state"] == "completed" and row["collection_status"] == "success":
            disposition = "reuse_successful_acquisition"
        elif row:
            disposition = "explicit_review_before_future_acquisition"
        else:
            disposition = "not_yet_attempted"
        result.append(
            {**target, "v1_state": dict(row) if row else None, "v2_disposition": disposition}
        )
    return result


def evaluate(old, new, a, b):
    stored = {
        row["job_id"]: row
        for row in a.execute(
            "SELECT job_id,decision,rejection_reasons_json FROM job_matches WHERE client_id=?",
            (CLIENT,),
        )
    }
    stored.update(
        {
            row["job_id"]: row
            for row in b.execute(
                "SELECT m.job_id,m.decision,m.rejection_reasons_json FROM job_matches m "
                "JOIN jobs j ON j.id=m.job_id WHERE j.source='workday' AND m.client_id=?",
                (CLIENT,),
            )
        }
    )
    a_identity = {
        row[0]: tuple(row)[1:]
        for row in a.execute(
            "SELECT id,source,source_board_id,source_job_id,content_fingerprint FROM jobs"
        )
    }
    b_identity = {
        row[0]: tuple(row)[1:]
        for row in b.execute(
            "SELECT id,source,source_board_id,source_job_id,content_fingerprint FROM jobs "
            "WHERE source!='workday'"
        )
    }
    require(a_identity == b_identity, "Stage A clone identity/fingerprint drift")
    require(len(a_identity) == 48373, "Stage A corpus changed")
    counters = {version: Counter() for version in ("v1", "v2")}
    by_source = {version: {} for version in counters}
    digests = {version: hashlib.sha256() for version in counters}
    sources = Counter()
    eligible = {version: [] for version in counters}
    incremental = []
    incremental_evidence = []
    for connection, query in (
        (a, "SELECT payload_json FROM jobs ORDER BY id"),
        (b, "SELECT payload_json FROM jobs WHERE source='workday' ORDER BY id"),
    ):
        for row in connection.execute(query):
            job = Job.model_validate_json(row[0])
            sources[job.source] += 1
            matches = {"v1": match_job(job, old), "v2": match_job(job, new)}
            persisted = stored.get(job.id)
            require(persisted is not None, f"missing persisted V1 match: {job.id}")
            require(
                matches["v1"].decision.value == persisted["decision"]
                and matches["v1"].rejection_reasons
                == json.loads(persisted["rejection_reasons_json"]),
                f"V1 control differs: {job.id}",
            )
            for version, match in matches.items():
                decision = match.decision.value
                counts = counters[version]
                source_counts = by_source[version].setdefault(job.source, Counter())
                for counter in (counts, source_counts):
                    counter["total"] += 1
                    counter[decision] += 1
                    passed = TITLE_REJECT not in match.rejection_reasons
                    counter["title_gate_passes"] += passed
                    counter["title_gate_rejects"] += not passed
                    counter["downstream_rejects"] += passed and decision == "reject"
                    counter["delivery_eligible"] += decision in ELIGIBLE
                record = match.model_dump(mode="json", exclude={"evaluated_at"})
                digests[version].update((canonical(record) + "\n").encode())
                if decision in ELIGIBLE:
                    eligible[version].append(job)
            if (
                matches["v2"].decision.value in ELIGIBLE
                and matches["v1"].decision.value not in ELIGIBLE
            ):
                incremental.append(job)
                incremental_evidence.append(
                    {
                        "job_id": job.id,
                        "source": job.source,
                        "source_board_id": job.source_board_id,
                        "source_job_id": job.source_job_id,
                        "title": job.title,
                        "company": job.company,
                        "canonical_url": str(job.canonical_url),
                        "content_fingerprint": job.content_fingerprint,
                        "v1": matches["v1"].model_dump(mode="json", exclude={"evaluated_at"}),
                        "v2": matches["v2"].model_dump(mode="json", exclude={"evaluated_at"}),
                    }
                )
    return {
        "source_counts": dict(sources),
        "matching": {v: dict(c) for v, c in counters.items()},
        "matching_by_source": {
            v: {s: dict(c) for s, c in rows.items()} for v, rows in by_source.items()
        },
        "matching_evidence_sha256": {v: h.hexdigest() for v, h in digests.items()},
        "matching_hash_order": "Stage A job ID, then Stage B Workday job ID; canonical JobMatch sans evaluated_at",
        "v1_control_matches_persisted_decisions_and_rejections": True,
        "v1_delivery_projection": delivery_projection(eligible["v1"], b),
        "v2_delivery_projection": delivery_projection(eligible["v2"], b),
        "incremental_delivery_projection": delivery_projection(incremental, b),
        "incremental_eligible_by_source": dict(Counter(job.source for job in incremental)),
        "incremental_eligible_evidence": sorted(
            incremental_evidence, key=lambda row: row["job_id"]
        ),
    }


def verify_results(result):
    require(
        result["source_counts"]
        == {
            "greenhouse": 25404,
            "ashby": 16047,
            "lever": 6922,
            "workday": 337,
        },
        "acquisition corpus does not reconcile to JOB-28",
    )
    for version, expected in {
        "v1": {"total": 48710, "title_gate_passes": 269, "delivery_eligible": 48},
        "v2": {
            "total": 48710,
            "title_gate_passes": 324,
            "downstream_rejects": 200,
            "needs_review": 62,
            "possible_match": 12,
            "strong_match": 50,
            "delivery_eligible": 62,
        },
    }.items():
        require(
            all(result["matching"][version][k] == v for k, v in expected.items()),
            f"{version} JOB-28 reproduction failed",
        )
    incremental = result["incremental_delivery_projection"]
    require(
        incremental["eligible_postings"] == 14 and incremental["fresh_groups"] == 12,
        "JOB-28 incremental capacity differs",
    )
    require(
        result["incremental_eligible_by_source"] == {"greenhouse": 5, "ashby": 8, "lever": 1},
        "incremental source distribution differs",
    )
    require(
        result["v2_delivery_projection"]["selected_groups"] == incremental["selected_groups"],
        "incremental and full V2 fresh projection differ",
    )


def persist_outputs(outputs):
    # Preflight ALL files before any write; repeat invocation only verifies frozen evidence.
    encoded = {
        name: json.dumps(value, indent=2, sort_keys=True) + "\n" for name, value in outputs.items()
    }
    for name, text in encoded.items():
        path = OUTPUT / name
        require(not path.exists() or path.read_text() == text, f"refuse to overwrite {path}")
    for name, text in encoded.items():
        path = OUTPUT / name
        if not path.exists():
            with path.open("x") as handle:
                handle.write(text)


def main():
    old, new = briefs()
    inputs = input_fingerprints()
    existing = OUTPUT / "manifest.json"
    if existing.exists():
        saved = json.loads(existing.read_text())
        require(
            saved["inputs_sha256"] == inputs, "frozen inputs changed; use an independent revision"
        )
    frozen_v1_manifest("stage_a", A_SHA)
    b_manifest = frozen_v1_manifest("stage_b", B_SHA)
    a_summary = json.loads((V1_ROOT / "stage_a/summary.json").read_text())
    require(
        a_summary["manifest_sha256"] == A_SHA and a_summary["fresh_unique_deliveries"] == 44,
        "Stage A summary continuity mismatch",
    )
    with readonly(A_DB) as a, readonly(B_DB) as b:
        result = evaluate(old, new, a, b)
        verify_results(result)
        history_rows = b.execute(
            "SELECT COUNT(*) FROM historical_job_links WHERE client_id=?", (CLIENT,)
        ).fetchone()[0]
        require(history_rows == 11107, "operator history continuity mismatch")
        imports = [
            dict(row)
            for row in b.execute(
                "SELECT client_id,workbook_sha256,row_count,importer_version FROM historical_imports "
                "WHERE client_id=? ORDER BY workbook_sha256",
                (CLIENT,),
            )
        ]
        markers = [
            dict(row)
            for row in b.execute(
                "SELECT group_id,job_id FROM group_deliveries WHERE client_id=? AND destination=? "
                "ORDER BY group_id",
                (CLIENT, DESTINATION),
            )
        ]
        require(len(markers) == 44, "cumulative V1 delivery marker baseline differs")
        states = [
            dict(row)
            for row in b.execute(
                "SELECT target_identity,state,collection_status FROM stage_b_target_runs "
                "WHERE manifest_sha256=? ORDER BY target_identity",
                (B_SHA,),
            )
        ]
        targets = handoff_targets(b_manifest["targets"], states)
        counts = dict(Counter(t["v2_disposition"] for t in targets))
        require(
            counts
            == {
                "reuse_successful_acquisition": 14,
                "explicit_review_before_future_acquisition": 1,
                "not_yet_attempted": 521,
            },
            "Workday acquisition states changed",
        )
        workday = {
            row["source_board_id"]: row["n"]
            for row in b.execute(
                "SELECT source_board_id,COUNT(*) n FROM jobs WHERE source='workday' GROUP BY source_board_id"
            )
        }
        for target in targets:
            count = workday.pop(target["target_identity"].removeprefix("workday:"), 0)
            target["acquired_postings"] = count
            if target["v2_disposition"] != "reuse_successful_acquisition":
                require(count == 0, "unreconciled incomplete acquisition")
        require(not workday, "Workday jobs outside frozen target universe")
    verify_input_fingerprints(inputs)
    manifest = {
        "validation": "taiwo-sourcing-capacity-v2-offline-baseline",
        "brief_revision": REVISION,
        "brief_schema_version": new.brief_version,
        "operator_client_id": CLIENT,
        "cumulative_destination": DESTINATION,
        "production_baseline_commit": "01daece87f727378245b34732a342e19a3626ea1",
        "matcher_version": MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "inputs_sha256": inputs,
        "v1_brief_sha256": file_sha(V1),
        "v2_brief_sha256": file_sha(V2),
        "acquisition": "frozen Stage A jobs plus 337 Stage B Workday jobs; no recollection",
        "matching": "both briefs recomputed in memory; no job_matches writes",
        "source_counts": result["source_counts"],
        "v1_stage_a_manifest_sha256": A_SHA,
        "v1_stage_b_manifest_sha256": B_SHA,
    }
    manifest["manifest_sha256"] = sha_value(manifest)
    summary = {
        "manifest_sha256": manifest["manifest_sha256"],
        "operator_client_id": CLIENT,
        "brief_revision": REVISION,
        "v1_brief_sha256": file_sha(V1),
        "v2_brief_sha256": file_sha(V2),
        "capacity": {
            "v1_fresh_baseline": 44,
            "additional_v2_fresh_groups": 12,
            "equivalent_v2_fresh_baseline": 56,
            "existing_workday_v2_incremental": 0,
            "business_threshold": 200,
            "preferred_threshold": 250,
            "remaining_to_business_threshold": 144,
            "remaining_to_preferred_threshold": 194,
        },
        "scope": "one acquired snapshot; equivalent baseline, not new actual deliveries or five-day capacity",
        "historical_rows": history_rows,
        "historical_imports": imports,
        "v1_stage_a_delivery_accounting": {
            k: a_summary[k]
            for k in ("historical_suppression", "practical_dedupe", "fresh_unique_deliveries")
        },
        "inherited_cumulative_markers": markers,
        "workday_acquisition_dispositions": counts,
        **result,
    }
    persist_outputs(
        {
            "manifest.json": manifest,
            "summary.json": summary,
            "workday_handoff.json": {
                "manifest_sha256": manifest["manifest_sha256"],
                "network_authorized": False,
                "targets": targets,
            },
        }
    )
    print(
        json.dumps(
            {
                "manifest_sha256": manifest["manifest_sha256"],
                "capacity": summary["capacity"],
                "matching": result["matching"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
