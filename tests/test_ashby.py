import csv
import hashlib
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from job_scout import cli
from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import JobLifecycle, SearchBrief, SourceTarget
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.sqlite import SQLiteRepository

TARGET = SourceTarget(board_id="acme", company="Acme")
PROVIDER_ID = "af12c78b-61dd-4e30-8ca7-5d8b34bfc253"


def raw_job(**changes):
    raw = {
        "id": PROVIDER_ID,
        "title": "Support Engineer",
        "location": "Remote, US",
        "secondaryLocations": [],
        "department": "Support",
        "team": "Customer Engineering",
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Remote",
        "descriptionPlain": "Troubleshoot customer systems and investigate Linux faults. " * 12,
        "descriptionHtml": "<p>Different HTML version</p>",
        "publishedAt": "2026-09-03T12:00:00Z",
        "employmentType": "FullTime",
        "address": {"postalAddress": {"addressCountry": "USA"}},
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{PROVIDER_ID}?utm_source=example",
        "applyUrl": f"https://jobs.ashbyhq.com/acme/{PROVIDER_ID}/application?ref=example",
    }
    raw.update(changes)
    return raw


def collector(jobs=None, *, payload=None, status=200, headers=None, text=None):
    body = payload if payload is not None else {"apiVersion": "1", "jobs": jobs or []}

    def respond(request):
        assert request.method == "GET"
        assert request.url.path == "/posting-api/job-board/acme"
        assert dict(request.url.params) == {"includeCompensation": "false"}
        assert "authorization" not in request.headers
        return (
            httpx.Response(status, json=body, headers=headers)
            if text is None
            else httpx.Response(status, text=text, headers=headers)
        )

    return AshbyCollector(httpx.Client(transport=httpx.MockTransport(respond)))


def normalized(**changes):
    result = collector([raw_job(**changes)]).collect(TARGET)
    assert result.status == "success", result.errors
    return result.jobs[0]


def test_valid_public_board_and_identity():
    job = normalized()
    assert job.source == "ashby" and job.source_board_id == "acme"
    assert job.source_job_id == PROVIDER_ID
    assert job.id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"ashby:acme:{PROVIDER_ID}"))
    assert job.raw_metadata["identity_source"] == "provider_id"
    assert job.country == "United States"
    assert job.department == "Support"
    assert job.raw_metadata["team"] == "Customer Engineering"
    assert job.posted_at.isoformat() == "2026-09-03T12:00:00+00:00"
    assert job.updated_at is None


@pytest.mark.parametrize("provider_id", [None, "", "  ", {}, False])
def test_job_url_identity_fallback(provider_id):
    job = normalized(id=provider_id)
    assert job.source_job_id == PROVIDER_ID
    assert job.raw_metadata["identity_source"] == "job_url"
    assert job.id == normalized().id


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.ashbyhq.com/acme",
        "https://jobs.ashbyhq.com/acme/support-engineer",
        f"https://example.com/acme/{PROVIDER_ID}",
        f"https://jobs.ashbyhq.com/other/{PROVIDER_ID}",
    ],
)
def test_missing_stable_identity_only_quarantines_one(url):
    result = collector([raw_job(), raw_job(id=None, jobUrl=url)]).collect(TARGET)
    assert result.status == "partial" and len(result.jobs) == 1
    assert "missing stable posting identity" in result.errors[0]


def test_explicit_provider_token_does_not_depend_on_url_fallback():
    assert (
        normalized(id="stable-provider-token", jobUrl="https://example.com/vacancy/1").source_job_id
        == "stable-provider-token"
    )


def test_primary_secondary_and_wrapped_secondary_country_evidence():
    job = normalized(
        secondaryLocations=[
            {
                "location": "Toronto",
                "address": {"addressCountry": "CAN", "addressLocality": "Toronto"},
            },
            {"location": "Lagos", "address": {"postalAddress": {"addressCountry": "Nigeria"}}},
        ]
    )
    assert job.eligible_countries == {"United States", "Canada", "Nigeria"}
    assert job.country is None
    assert len(job.raw_metadata["secondaryLocations"]) == 2


def test_structured_evidence_precedes_conflicting_text_per_location():
    job = normalized(location="Remote, Canada", secondaryLocations=[{"location": "India"}])
    assert job.eligible_countries == {"United States", "India"}


@pytest.mark.parametrize("location", ["AMER", "EMEA", "Global", "Remote", "Africa", "APAC"])
def test_vague_locations_do_not_establish_country(location):
    job = normalized(address=None, location=location)
    assert not job.eligible_countries and job.country is None


def test_primary_city_and_region_are_direct_facts():
    job = normalized(
        address={
            "postalAddress": {
                "addressCountry": "USA",
                "addressLocality": "Houston",
                "addressRegion": "Texas",
            }
        }
    )
    assert (job.city, job.region) == ("Houston", "Texas")


@pytest.mark.parametrize(
    "country,expected",
    [
        ("Brazil", {"Brazil"}),
        ("European Union", set()),
        ("AMER", set()),
        ("EMEA", set()),
        ("APAC", set()),
        ("US | EU", {"United States"}),
    ],
)
def test_structured_country_names_and_regions(country, expected):
    assert (
        normalized(
            address={"postalAddress": {"addressCountry": country}}, location=None
        ).eligible_countries
        == expected
    )


@pytest.mark.parametrize(
    "workplace,is_remote,expected",
    [
        ("Remote", True, "remote"),
        ("Hybrid", True, "hybrid"),
        ("OnSite", True, "onsite"),
        (None, True, "remote"),
        (None, None, "unknown"),
        (None, False, "unknown"),
        ("NewProviderValue", True, "unknown"),
    ],
)
def test_workplace_precedence(workplace, is_remote, expected):
    job = normalized(
        workplaceType=workplace,
        isRemote=is_remote,
        location="Engineering",
        descriptionPlain="Support customers",
        descriptionHtml=None,
    )
    assert job.remote_status == expected


def test_textual_remote_fallback_only_when_flags_absent():
    assert (
        normalized(workplaceType=None, isRemote=None, location="Remote, US").remote_status
        == "remote"
    )


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("FullTime", "full_time"),
        ("PartTime", "part_time"),
        ("Contract", "contract"),
        ("Temporary", "temporary"),
        ("Intern", "internship"),
        ("NewEnum", None),
        (None, None),
    ],
)
def test_employment_mapping(provider, expected):
    job = normalized(employmentType=provider)
    assert job.employment_type == expected
    assert job.raw_metadata["employmentType"] == provider


def test_plain_description_preferred_and_not_truncated():
    description = "Full plain evidence. " * 600
    job = normalized(descriptionPlain=description)
    assert job.description_text == description
    assert job.description_html == "<p>Different HTML version</p>"


@pytest.mark.parametrize("plain", [None, "", "   "])
def test_html_description_fallback(plain):
    job = normalized(
        descriptionPlain=plain, descriptionHtml="<p>Linux &amp; Python</p><script>secret</script>"
    )
    assert job.description_text == "Linux & Python"


def test_job_and_apply_urls_are_preserved_and_canonicalized():
    job = normalized()
    assert (
        str(job.job_url) == str(job.canonical_url) == f"https://jobs.ashbyhq.com/acme/{PROVIDER_ID}"
    )
    assert str(job.apply_url) == f"https://jobs.ashbyhq.com/acme/{PROVIDER_ID}/application"


def test_unlisted_skipped_without_schema_failure_and_counts_reset():
    source = collector([raw_job(), {"isListed": False}])
    for _ in range(2):
        result = source.collect(TARGET)
        assert result.status == "success" and len(result.jobs) == 1
        assert source.last_counts == {
            "received": 2,
            "skipped_unlisted": 1,
            "normalized": 1,
            "quarantined": 0,
        }


@pytest.mark.parametrize("malformed", [{"title": "Missing URL"}, None, {"id": "x", "title": ""}])
def test_bad_item_is_partial(malformed):
    result = collector([raw_job(), malformed]).collect(TARGET)
    assert result.status == "partial" and len(result.jobs) == len(result.errors) == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        (404, "invalid_target"),
        (429, "rate_limited"),
        (401, "authentication_failure"),
        (403, "forbidden"),
        (500, "provider_error"),
        (503, "provider_error"),
    ],
)
def test_http_failures(status, expected):
    result = collector(status=status).collect(TARGET)
    assert result.status == expected and result.errors and not result.jobs


@pytest.mark.parametrize(
    "headers,text", [({"Retry-After": "20"}, ""), ({}, "rate limit exceeded"), ({}, "throttled")]
)
def test_throttled_403(headers, text):
    assert (
        collector(status=403, headers=headers, text=text).collect(TARGET).status == "rate_limited"
    )


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.ProxyError])
def test_network_failure(error):
    def fail(request):
        raise error("connection failed", request=request)

    source = AshbyCollector(httpx.Client(transport=httpx.MockTransport(fail)))
    assert source.collect(TARGET).status == "network_failure"


@pytest.mark.parametrize("payload", [{}, {"jobs": {}}, [], {"jobs": None}])
def test_invalid_top_level_schema(payload):
    assert collector(payload=payload).collect(TARGET).status == "parse_failure"


def test_invalid_json():
    assert collector(text="{oops").collect(TARGET).status == "parse_failure"


def test_empty_board_success():
    result = collector([]).collect(TARGET)
    assert result.status == "success" and not result.jobs and not result.errors


def test_unknown_fields_ignored_and_metadata_allowlisted():
    job = normalized(
        candidate={"email": "private@example.org"},
        future_field={"value": 1},
        address={"postalAddress": {"addressCountry": "USA", "private": "secret"}},
    )
    assert "private@example.org" not in job.model_dump_json()
    assert "secret" not in job.model_dump_json()
    assert "future_field" not in job.raw_metadata


@pytest.mark.parametrize(
    "changes",
    [
        {"descriptionPlain": "Changed content"},
        {"workplaceType": "Hybrid"},
        {"secondaryLocations": [{"location": "Canada"}]},
        {"team": "New team"},
        {"publishedAt": "2026-09-04T12:00:00Z"},
    ],
)
def test_persistence_rerun_and_meaningful_change(tmp_path, changes):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first = normalized()
    assert repo.upsert_job(first) is JobLifecycle.NEW
    assert repo.upsert_job(normalized()) is JobLifecycle.SEEN
    changed = normalized(**changes)
    assert changed.id == first.id
    assert repo.upsert_job(changed) is JobLifecycle.CHANGED
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_cross_source_pipeline_group_and_delivery(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    brief = SearchBrief(client_id="client", target_roles=["Support Engineer"])
    output = tmp_path / "leads.csv"
    ashby_raw = raw_job()
    greenhouse_raw = {
        "jobs": [
            {
                "id": 1,
                "title": "Support Engineer",
                "location": {"name": "Remote, US"},
                "absolute_url": "https://example.com/jobs/1",
                "content": ashby_raw["descriptionPlain"],
                "departments": [{"name": "Support"}],
                "metadata": [{"name": "Employment Type", "value": "full time"}],
            }
        ]
    }
    gh = GreenhouseCollector(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=greenhouse_raw))
        )
    )
    first = run_pipeline(
        collector=gh, target=TARGET, profile=brief, repository=repo, csv_path=output
    )
    second = run_pipeline(
        collector=collector([ashby_raw]),
        target=TARGET,
        profile=brief,
        repository=repo,
        csv_path=output,
    )
    assert first.exported == 1 and second.matched == 1 and second.exported == 0
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM delivery_groups").fetchone()[0] == 1
        assert {r[0] for r in c.execute("SELECT source FROM jobs")} == {"greenhouse", "ashby"}
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    # Same architecture preserves a genuinely different posting as a separate lead.
    different = raw_job(
        id="another-id",
        jobUrl="https://jobs.ashbyhq.com/acme/other",
        applyUrl=None,
        descriptionPlain="Different database support responsibilities. " * 12,
    )
    assert (
        run_pipeline(
            collector=collector([different]),
            target=TARGET,
            profile=brief,
            repository=repo,
            csv_path=output,
        ).exported
        == 1
    )
    with output.open() as handle:
        rows = list(csv.DictReader(handle))
        assert [row["Job Platform"] for row in rows] == ["Greenhouse", "Ashby"]


def test_preserved_audit_examples_ingest():
    examples = json.loads(
        (Path(__file__).parent / "fixtures/ashby_audit_examples.json").read_text()
    )
    for example in examples:
        payload = {"apiVersion": "1", "jobs": [example["job"]]}
        source = AshbyCollector(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, body=payload: httpx.Response(200, json=body)
                )
            )
        )
        result = source.collect(SourceTarget(board_id=example["board"], company=example["board"]))
        assert result.status == "success" and len(result.jobs) == 1
    assert result.jobs[0].remote_status == "hybrid"


@pytest.mark.parametrize("source", [None, "greenhouse", "ashby"])
def test_cli_routes_single_source_backwards_compatibly(tmp_path, monkeypatch, capsys, source):
    brief = tmp_path / "brief.json"
    brief.write_text(
        SearchBrief(client_id="test", target_roles=["Support Engineer"]).model_dump_json()
    )
    selected = []

    def pipeline(**kwargs):
        selected.append(kwargs["collector"].source)
        from job_scout.orchestration.pipeline import PipelineSummary

        return PipelineSummary()

    monkeypatch.setattr(cli, "run_pipeline", pipeline)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "collect",
            "--client",
            str(brief),
            "--board",
            "acme",
            "--company",
            "Acme",
            "--database",
            str(tmp_path / "jobs.db"),
        ]
        + (["--source", source] if source else []),
    )
    cli.main()
    assert selected == [source or "greenhouse"]
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_frozen_matcher_dedupe_and_brief_bytes_unchanged():
    # Git object hashes of the accepted e20787a baseline; intentional freeze gate.
    expected = {
        "job_scout/matching/matcher.py": "e21ab3d271210686104f2b7786197951fde1fc80",
        "job_scout/dedupe/resolver.py": "42068e1657b3bf73e7516f1101867d20ee47268d",
        "config/search_briefs/taiwo_operator_sourcing_v1.json": "5ea6ba5ea8d5dbabf85dd1d9b35e40896dc5edf8",
    }
    for name, expected_hash in expected.items():
        data = Path(name).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == expected_hash
