from job_scout.storage.sqlite import SQLiteRepository


class FakeConnection:
    def __init__(self) -> None:
        self.row_factory = None
        self.push_calls = 0
        self.commit_calls = 0
        self.close_calls = 0
        self.executed: list[str] = []

    def execute(self, statement, *_args):
        self.executed.append(statement)
        return self

    def commit(self) -> None:
        self.commit_calls += 1

    def push(self) -> None:
        self.push_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakePrefixBootstrap:
    def __init__(self, *, length: int) -> None:
        self.length = length


class FakePartialSyncOpts:
    def __init__(self, *, bootstrap_strategy) -> None:
        self.bootstrap_strategy = bootstrap_strategy


class FakeSync:
    PartialSyncPrefixBootstrap = FakePrefixBootstrap
    PartialSyncOpts = FakePartialSyncOpts

    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.kwargs = None

    def connect(self, path, **kwargs):
        self.path = path
        self.kwargs = kwargs
        return self.connection


def repository_with_fake_sync() -> tuple[SQLiteRepository, FakeSync]:
    sync = FakeSync()
    repository = object.__new__(SQLiteRepository)
    repository.path = "/tmp/jobsift.sqlite3"
    repository.remote_url = "https://jobsift.example.turso.io"
    repository.auth_token = "secret"
    repository._turso_sync = sync
    repository._turso_error = ()
    return repository, sync


def test_turso_connections_use_partial_bootstrap_instead_of_full_replica():
    repository, sync = repository_with_fake_sync()

    with repository.connect(push=False):
        pass

    options = sync.kwargs["partial_sync_experimental"]
    assert options.bootstrap_strategy.length == 128 * 1024
    assert sync.connection.push_calls == 0


def test_read_only_connection_can_skip_remote_push_but_writes_still_push():
    repository, sync = repository_with_fake_sync()

    with repository.connect(push=False) as connection:
        connection.execute("SELECT 1")
    assert sync.connection.push_calls == 0

    with repository.connect() as connection:
        connection.execute("INSERT INTO example VALUES (1)")
    assert sync.connection.push_calls == 1
