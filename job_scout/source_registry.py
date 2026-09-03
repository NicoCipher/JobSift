from datetime import UTC, datetime

from job_scout.domain.models import SourceRegistryEntry

GREENHOUSE = SourceRegistryEntry(
    name="greenhouse",
    enabled=True,
    source_type="ats",
    access_mode="public_api",
    official_api=True,
    base_url="https://boards-api.greenhouse.io/v1/boards/",
    authentication_required=False,
    attribution_required=None,
    commercial_use_notes="GET access is documented as public; downstream use must still respect provider terms.",
    rate_limit_notes="No numeric GET rate limit published in the referenced Job Board API documentation.",
    supports_direct_apply_url=True,
    supports_description=True,
    supports_location=True,
    supports_remote_flag=False,
    supports_posted_at=False,
    supports_updated_at=True,
    last_verified_at=datetime(2026, 9, 3, tzinfo=UTC),
    documentation_reference="https://docs.greenhouse.io/job-board.html",
)
