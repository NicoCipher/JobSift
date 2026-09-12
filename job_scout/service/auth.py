"""Trusted loopback identity and bounded per-process read admission, never header identity."""

from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from job_scout.service.errors import ServiceError


class Admission:
    def __init__(self, config):
        self.host = urlsplit(config.origin).netloc
        self.origin = config.origin
        self.lock = Lock()
        self.tokens = 20.0
        self.updated = monotonic()
        self.active = 0

    def enter(self, request):
        server = request.scope.get("server")
        if server and server[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise ServiceError("FORBIDDEN")
        if request.headers.get("host") != self.host:
            raise ServiceError("FORBIDDEN")
        if request.headers.get("origin") not in {None, self.origin}:
            raise ServiceError("FORBIDDEN")
        with self.lock:
            now = monotonic()
            self.tokens = min(20, self.tokens + (now - self.updated) * 2)
            self.updated = now
            if self.tokens < 1 or self.active >= 10:
                raise ServiceError("RATE_LIMITED")
            self.tokens -= 1
            self.active += 1

    def leave(self):
        with self.lock:
            self.active -= 1
