from typing import Protocol

from job_scout.domain.models import CollectionResult, SourceTarget


class JobCollector(Protocol):
    source: str

    def collect(self, target: SourceTarget) -> CollectionResult: ...
