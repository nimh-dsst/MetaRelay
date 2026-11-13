"""The GitHub backend, run against the fake GitHub API over real HTTP.

The real ``httpx`` client talks to ``FakeGitHub``, so these tests exercise
the same code path as production. ``test_github_live.py`` runs the
conformance suite against real GitHub when a token is set.
"""

import json
from contextlib import closing

import pytest

from metarelay import (
    ROOT,
    AuthenticationError,
    BackendUnavailableError,
    ConfigurationError,
    ConflictError,
    LogicalPath,
    NotFoundError,
    PermissionDeniedError,
    TokenConfig,
    ValidationError,
    WriteMode,
    auth_flow,
    begin_login,
    connect,
)
from metarelay.core.auth import AuthFlow
from metarelay.testing import BackendConformance
from metarelay.testing.github_fake import FakeGitHub, git_blob_sha, serve_fake

pytest.importorskip("httpx")

from metarelay.backends.github import (
    INDEX_FILE,
    GitHubConfig,
    GitHubSession,
)

TOKEN = "test-token"
REPO = "my-lab/metadata"
URI = f"github://{REPO}@main/records"
STUDY = LogicalPath(("Study",))


@pytest.fixture
def fake() -> FakeGitHub:
    fake = FakeGitHub(token=TOKEN)
    fake.add_repo(REPO, branches=("main", "feature/demo"), files={"README.md": "# Metadata\n"})
    return fake


@pytest.fixture
def api_url(fake: FakeGitHub):
    with serve_fake(fake) as url:
        yield url


def config_for(api_url: str, **overrides) -> GitHubConfig:
    return GitHubConfig(**{"token": TOKEN, "api_url": api_url, **overrides})


def open_session(api_url: str, uri: str = URI, **overrides) -> GitHubSession:
    return connect(uri, config_for(api_url, **overrides))


class TestGitHubBackend(BackendConformance):
    @pytest.fixture
    def session(self, fake: FakeGitHub, api_url: str):
        self.fake = fake
        session = open_session(api_url)
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        receipt = session.read_record(path, record_id).receipt
        md_path = receipt.backend_ids["path"].removesuffix(".json") + ".md"
        self.fake.put_file(REPO, md_path, text)


@pytest.fixture
def session(api_url: str):
    session = open_session(api_url)
    session.ensure_container(STUDY)
    yield session
    session.close()


# Connecting


def test_uses_the_token_flow(api_url: str) -> None:
    assert auth_flow(URI) is AuthFlow.TOKEN
    with pytest.raises(ConfigurationError, match="browser login"):
        begin_login(URI, "http://localhost/callback", config_for(api_url))


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("github://my-lab/metadata@main/records", "github://my-lab/metadata@main/records"),
        ("github://My-Lab/MetaData@main/records/", "github://my-lab/metadata@main/records"),
        ("github://my-lab/metadata@main", "github://my-lab/metadata@main"),
        ("github://my-lab/metadata@main/lab%20data", "github://my-lab/metadata@main/lab%20data"),
        (
            "github://my-lab/metadata@feature%2Fdemo/records",
            "github://my-lab/metadata@feature%2Fdemo/records",
        ),
    ],
)
def test_destination_is_canonical(api_url: str, uri: str, expected: str) -> None:
    with closing(open_session(api_url, uri)) as session:
        assert session.destination == expected


@pytest.mark.parametrize(
    "uri",
    [
        "github://my-lab/metadata",  # no branch
        "github://my-lab/metadata@",  # empty branch
        "github://my-lab@main",  # no repository
        "github:///metadata@main",  # no owner
        "github://my-lab/metadata@main/records?x=1",
        "github://my-lab/metadata@main/a/../b",
        "github://my_lab/metadata@main",  # not a GitHub owner name
    ],
)
def test_bad_destinations_are_configuration_errors(api_url: str, uri: str) -> None:
    with pytest.raises(ConfigurationError):
        open_session(api_url, uri)


def test_a_missing_repository_is_reported(api_url: str) -> None:
    with pytest.raises(ConfigurationError, match="no repository my-lab/missing"):
        open_session(api_url, "github://my-lab/missing@main")


def test_a_missing_branch_is_reported(api_url: str) -> None:
    with pytest.raises(ConfigurationError, match="no branch 'nope'"):
        open_session(api_url, "github://my-lab/metadata@nope")


def test_a_bad_token_is_an_authentication_error(api_url: str) -> None:
    with pytest.raises(AuthenticationError):
        open_session(api_url, token="wrong")


def test_the_token_is_required(monkeypatch) -> None:
    monkeypatch.delenv("METARELAY_GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="METARELAY_GITHUB_TOKEN"):
        connect(URI)


def test_config_reads_its_own_environment_variables(monkeypatch, api_url: str) -> None:
    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("METARELAY_GITHUB_API_URL", api_url)
    session = connect(URI)
    session.close()


def test_a_plain_token_config_works(monkeypatch, api_url: str) -> None:
    monkeypatch.setenv("METARELAY_GITHUB_API_URL", api_url)
    session = connect(URI, TokenConfig(token=TOKEN))
    session.close()


def test_other_configs_are_refused(api_url: str) -> None:
    from metarelay.core.config import DestinationConfig

    with pytest.raises(ConfigurationError, match="GitHubConfig"):
        connect(URI, DestinationConfig())


def test_the_token_is_never_shown() -> None:
    config = GitHubConfig(token="github_pat_secret", api_url="https://api.github.com")
    assert "github_pat_secret" not in repr(config)
    assert "github_pat_secret" not in config.model_dump_json()


def test_closed_session_refuses_requests(api_url: str) -> None:
    session = open_session(api_url)
    session.close()
    with pytest.raises(ConfigurationError, match="closed"):
        session.list_containers()


# Layout


def test_records_are_json_and_a_markdown_preview(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    record = make_record()
    receipt = session.write_record(STUDY, record)

    files = fake.files(REPO)
    stored = files["records/Study/fiber-1.json"]
    assert json.loads(stored)["record_id"] == "fiber-1"
    preview = files["records/Study/fiber-1.md"].decode()
    assert "| Field | Value |" in preview
    assert "fiber-1" in preview
    assert receipt.conflict_token == git_blob_sha(stored)
    assert receipt.backend_ids["path"] == "records/Study/fiber-1.json"
    # README.md outside the prefix is untouched.
    assert files["README.md"] == b"# Metadata\n"


def test_ensure_container_writes_a_placeholder(api_url: str, fake: FakeGitHub) -> None:
    session = open_session(api_url)
    session.ensure_container(LogicalPath(("A", "B")))
    assert f"records/A/B/{INDEX_FILE}" in fake.files(REPO)
    assert [c.path for c in session.list_containers(LogicalPath(("A",)))] == [
        LogicalPath(("A", "B"))
    ]
    session.close()


def test_the_root_exists_before_the_prefix_does(api_url: str) -> None:
    session = open_session(api_url, "github://my-lab/metadata@main/not-yet")
    assert session.list_containers() == []
    assert session.list_records(ROOT) == []
    session.close()


def test_writes_go_to_the_named_branch(api_url: str, fake: FakeGitHub, make_record) -> None:
    session = open_session(api_url, "github://my-lab/metadata@feature%2Fdemo/records")
    session.ensure_container(STUDY)
    session.write_record(STUDY, make_record())
    assert "records/Study/fiber-1.json" in fake.files(REPO, "feature/demo")
    assert "records/Study/fiber-1.json" not in fake.files(REPO, "main")
    session.close()


def test_hidden_files_are_not_containers(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.put_file(REPO, "records/.github/workflows/x.yml", "on: push\n")
    assert [c.path for c in session.list_containers()] == [STUDY]


def test_identical_write_makes_no_commit(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    commits = len(fake.repo(REPO).commits)
    session.write_record(STUDY, make_record(), prior_receipt=receipt)
    session.write_record(STUDY, make_record())
    assert len(fake.repo(REPO).commits) == commits


def test_a_missing_preview_is_restored(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record())
    fake.delete_file(REPO, "records/Study/fiber-1.md")
    session.write_record(STUDY, make_record())
    assert "records/Study/fiber-1.md" in fake.files(REPO)


def test_records_without_an_index_are_still_found(
    api_url: str, fake: FakeGitHub, make_record
) -> None:
    record = make_record()
    fake.put_file(REPO, "records/Hand made/fiber-1.json", record.model_dump_json())
    session = open_session(api_url)
    [stored] = session.list_records(LogicalPath(("Hand made",)))
    assert stored.record == record
    assert stored.receipt.preview_checksum is None
    session.close()


def test_a_damaged_index_is_reported(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.put_file(REPO, f"records/Study/{INDEX_FILE}", "not json")
    with pytest.raises(ValidationError, match="damaged"):
        session.list_records(STUDY)


def test_a_damaged_record_is_reported(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.put_file(REPO, "records/Study/broken.json", "{}")
    with pytest.raises(ValidationError, match="not a valid record"):
        session.read_record(STUDY, "broken")


def test_case_twins_are_refused(session: GitHubSession) -> None:
    with pytest.raises(ValidationError, match="differs only in case"):
        session.ensure_container(LogicalPath(("study",)))
    with pytest.raises(ValidationError, match="differs only in case"):
        session.list_records(LogicalPath(("study",)))


def test_a_file_is_not_a_container(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.put_file(REPO, "records/notes.txt", "hi")
    with pytest.raises(ValidationError, match="is a file"):
        session.ensure_container(LogicalPath(("notes.txt", "child")))


# Conflicts use the blob sha


def test_a_change_between_read_and_write_is_a_conflict(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record(diameter_um=1))
    real_listing = session._listing

    def listing_then_someone_writes(path):
        entries = real_listing(path)
        if path == STUDY:
            fake.put_file(
                REPO, "records/Study/fiber-1.json", make_record(diameter_um=3).model_dump_json()
            )
        return entries

    session._listing = listing_then_someone_writes
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(diameter_um=2), mode=WriteMode.FORCE)
    session._listing = real_listing
    assert session.read_record(STUDY, "fiber-1").record.payload["diameter_um"] == 3


def test_create_is_enforced_by_github(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    real_listing = session._listing

    def listing_then_someone_creates(path):
        entries = real_listing(path)
        if path == STUDY:
            fake.put_file(REPO, "records/Study/fiber-1.json", make_record().model_dump_json())
        return entries

    session._listing = listing_then_someone_creates
    with pytest.raises(ConflictError, match="already exists"):
        session.write_record(STUDY, make_record(diameter_um=2), mode=WriteMode.CREATE)


def test_a_concurrent_index_update_is_retried(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record("fiber-1"))
    real_put = session._put
    raced = []

    def put_after_someone_else(git_path, data, **kwargs):
        if git_path.endswith(INDEX_FILE) and not raced:
            raced.append(True)
            index = json.loads(fake.files(REPO)[git_path])
            index["files"].append("other.csv")
            fake.put_file(REPO, git_path, json.dumps(index))
        return real_put(git_path, data, **kwargs)

    session._put = put_after_someone_else
    session.write_record(STUDY, make_record("fiber-2"))
    index = json.loads(fake.files(REPO)[f"records/Study/{INDEX_FILE}"])
    assert set(index["records"]) == {"fiber-1", "fiber-2"}
    assert index["files"] == ["other.csv"]


# Files


def test_files_over_the_cap_are_refused(api_url: str, fake: FakeGitHub) -> None:
    session = open_session(api_url, max_file_bytes=1024)
    session.ensure_container(STUDY)
    session.write_file(STUDY, "small.csv", b"x" * 1024)
    with pytest.raises(ValidationError, match=r"1\.0 KB.*max_file_bytes"):
        session.write_file(STUDY, "big.bin", b"x" * 1025)
    assert "records/Study/big.bin" not in fake.files(REPO)
    session.close()


def test_the_default_cap_is_one_megabyte(session: GitHubSession) -> None:
    with pytest.raises(ValidationError, match=r"1\.0 MB"):
        session.write_file(STUDY, "big.bin", b"x" * (1024 * 1024 + 1))


def test_the_cap_cannot_exceed_what_github_accepts() -> None:
    with pytest.raises(ConfigurationError):
        GitHubConfig(token=TOKEN, max_file_bytes=200 * 1024 * 1024)


def test_write_file_receipt_has_the_blob_sha(session: GitHubSession, fake: FakeGitHub) -> None:
    receipt = session.write_file(STUDY, "trace.csv", b"t,s\n0,1\n")
    assert receipt.native_id == git_blob_sha(b"t,s\n0,1\n")
    assert fake.files(REPO)["records/Study/trace.csv"] == b"t,s\n0,1\n"


# Errors


def test_server_errors_are_backend_unavailable(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.fail_next(502)
    with pytest.raises(BackendUnavailableError, match="server error"):
        session.list_containers()


def test_rate_limits_are_backend_unavailable(session: GitHubSession, fake: FakeGitHub) -> None:
    fake.fail_next(403, "API rate limit exceeded", {"x-ratelimit-remaining": "0"})
    with pytest.raises(BackendUnavailableError, match="rate limit"):
        session.list_containers()


def test_read_only_tokens_are_permission_denied(
    api_url: str, fake: FakeGitHub, make_record
) -> None:
    fake.repo(REPO).can_push = False
    session = open_session(api_url)  # reading still works
    assert session.list_containers() == []
    with pytest.raises(PermissionDeniedError, match="Contents: read and write"):
        session.ensure_container(STUDY)
    session.close()


def test_unreachable_github_is_backend_unavailable() -> None:
    with pytest.raises(BackendUnavailableError, match="Could not reach GitHub"):
        open_session("http://127.0.0.1:9", timeout=2)


def test_errors_never_include_the_token(api_url: str, fake: FakeGitHub) -> None:
    session = open_session(api_url)
    fake.fail_next(500)
    with pytest.raises(BackendUnavailableError) as caught:
        session.list_containers()
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    session.close()


def test_missing_containers_are_not_found(session: GitHubSession) -> None:
    with pytest.raises(NotFoundError, match="does not exist"):
        session.list_records(LogicalPath(("Nope",)))


# Regressions from review


def test_a_deleted_branch_is_not_an_empty_destination(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    del fake.repo(REPO).branches["main"]
    with pytest.raises(NotFoundError, match="branch 'main'"):
        session.list_containers()


def test_lost_access_is_not_an_empty_destination(api_url: str, fake: FakeGitHub) -> None:
    session = open_session(api_url, "github://my-lab/metadata@main/not-yet")
    del fake.repos[REPO]
    with pytest.raises(NotFoundError):
        session.list_records(ROOT)
    session.close()


def test_a_malformed_blob_response_is_backend_unavailable(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record())
    session._blobs.clear()
    real_request = session._request

    def blob_as_a_list(method, url, action, **kwargs):
        if "/git/blobs/" in url:
            return []
        return real_request(method, url, action, **kwargs)

    session._request = blob_as_a_list
    with pytest.raises(BackendUnavailableError, match="did not understand"):
        session.read_record(STUDY, "fiber-1")


def test_an_indexed_record_deleted_by_hand_is_skipped(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record("fiber-1"))
    session.write_record(STUDY, make_record("fiber-2"))
    fake.delete_file(REPO, "records/Study/fiber-1.json")
    assert [item.record.record_id for item in session.list_records(STUDY)] == ["fiber-2"]


def test_a_protected_branch_is_not_a_conflict(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    fake.repo(REPO).protected.add("main")
    before = len(fake.requests)
    with pytest.raises(PermissionDeniedError, match="pull request"):
        session.write_record(STUDY, make_record())
    # Refused once, not retried as if it were a race.
    assert sum(method == "PUT" for method, _ in fake.requests[before:]) == 1


def test_a_failed_upload_leaves_the_index_alone(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    real_put = session._put

    def fail_the_upload(git_path, data, **kwargs):
        if git_path.endswith("trace.json"):
            raise BackendUnavailableError("network")
        return real_put(git_path, data, **kwargs)

    session._put = fail_the_upload
    with pytest.raises(BackendUnavailableError):
        session.write_file(STUDY, "trace.json", b"{}")
    session._put = real_put
    # The name was never written, so it must not block a record of that name.
    session.write_record(STUDY, make_record("trace"))
    assert [item.record.record_id for item in session.list_records(STUDY)] == ["trace"]


def test_a_bad_setting_is_named_not_blamed_on_the_token(monkeypatch) -> None:
    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("METARELAY_GITHUB_API_URL", "api.github.com")
    with pytest.raises(ConfigurationError, match="api_url"):
        connect(URI)


def test_foreign_json_does_not_break_listing(api_url: str, fake: FakeGitHub, make_record) -> None:
    fake.put_file(REPO, "package.json", '{"name": "lab-site"}')
    session = open_session(api_url, "github://my-lab/metadata@main")
    session.write_record(ROOT, make_record())
    assert [item.record.record_id for item in session.list_records(ROOT)] == ["fiber-1"]
    with pytest.raises(ValidationError, match="not a valid record"):
        session.read_record(ROOT, "package")  # asked for by name: still loud
    session.close()


def test_uploaded_files_are_not_kept_in_memory(session: GitHubSession) -> None:
    session.write_file(STUDY, "trace.csv", b"x" * 1000)
    assert all(len(data) < 1000 for data in session._blobs.values())


def _fail_put_to(session: GitHubSession, suffix: str, error: Exception):
    real_put = session._put

    def put(git_path, data, **kwargs):
        if git_path.endswith(suffix):
            raise error
        return real_put(git_path, data, **kwargs)

    session._put = put
    return real_put


@pytest.mark.parametrize("step", ["fiber-1.md", INDEX_FILE])
def test_a_write_that_fails_after_the_record_says_so(
    session: GitHubSession, fake: FakeGitHub, make_record, step: str
) -> None:
    first = session.write_record(STUDY, make_record())
    changed = make_record(diameter_um=200.0)
    real_put = _fail_put_to(session, step, BackendUnavailableError("network"))
    with pytest.raises(BackendUnavailableError, match="was saved") as caught:
        session.write_record(STUDY, changed, prior_receipt=first)
    session._put = real_put
    saved = caught.value.receipt
    assert saved.checksum != first.checksum
    assert session.read_record(STUDY, "fiber-1").receipt.checksum == saved.checksum

    # Writing again with that receipt finishes the job: the preview and the
    # index catch up, and a later read-then-write is not a false conflict.
    session.write_record(STUDY, changed, prior_receipt=saved)
    assert "200" in fake.files(REPO)["records/Study/fiber-1.md"].decode()
    read = session.read_record(STUDY, "fiber-1")
    session.write_record(STUDY, make_record(diameter_um=300.0), prior_receipt=read.receipt)


def test_a_preview_edited_mid_write_keeps_the_receipt(session: GitHubSession, make_record) -> None:
    first = session.write_record(STUDY, make_record())
    _fail_put_to(session, "fiber-1.md", ConflictError("sha does not match"))
    with pytest.raises(ConflictError, match="was saved") as caught:
        session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)
    assert caught.value.receipt.checksum != first.checksum
