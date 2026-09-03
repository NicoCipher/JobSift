from job_scout.domain.models import Job


def identity_key(job: Job) -> tuple[str, ...]:
    if job.source_job_id:
        return ("provider", job.source, job.source_board_id, job.source_job_id)
    return ("url", str(job.canonical_url))


def select_preferred_url(job: Job) -> str:
    return str(job.apply_url or job.canonical_url)
