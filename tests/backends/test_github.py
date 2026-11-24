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
    connect,
)
from metarelay.testing import BackendConformance
from metarelay.testing.github_fake import FakeGitHub, git_blob_sha, serve_fake

pytest.importorskip("httpx")

import metarelay.backends.github as github_backend
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

    def replace_in_preview(self, session, path, record_id, old, new) -> None:
        receipt = session.read_record(path, record_id).receipt
        md_path = receipt.backend_ids["path"].removesuffix(".json") + ".md"
        text = self.fake.files(REPO)[md_path].decode()
        self.fake.put_file(REPO, md_path, text.replace(old, new))


@pytest.fixture
def session(api_url: str):
    session = open_session(api_url)
    session.ensure_container(STUDY)
    yield session
    session.close()


# Connecting


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("github://My-Lab/MetaData@main/records/", "github://my-lab/metadata@main/records"),
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


def test_a_missing_repository_or_branch_is_reported(api_url: str) -> None:
    with pytest.raises(ConfigurationError, match="no repository my-lab/missing"):
        open_session(api_url, "github://my-lab/missing@main")
    with pytest.raises(ConfigurationError, match="no branch 'nope'"):
        open_session(api_url, "github://my-lab/metadata@nope")


def test_a_bad_token_is_an_authentication_error(api_url: str) -> None:
    with pytest.raises(AuthenticationError):
        open_session(api_url, token="wrong")


def test_config_errors_name_the_fix(monkeypatch) -> None:
    from metarelay.core.config import DestinationConfig

    monkeypatch.delenv("METARELAY_GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="METARELAY_GITHUB_TOKEN"):
        connect(URI)
    with pytest.raises(ConfigurationError, match="GitHubConfig"):
        connect(URI, DestinationConfig())
    # With the token set, a bad setting is named, not blamed on the token.
    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("METARELAY_GITHUB_API_URL", "api.github.com")
    with pytest.raises(ConfigurationError, match="api_url"):
        connect(URI)


def test_config_comes_from_the_environment_or_a_plain_token(monkeypatch, api_url: str) -> None:
    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("METARELAY_GITHUB_API_URL", api_url)
    connect(URI).close()
    monkeypatch.delenv("METARELAY_GITHUB_TOKEN")
    connect(URI, TokenConfig(token=TOKEN)).close()


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


def test_commits_per_write_are_one_one_then_zero(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    commits = fake.repo(REPO).commits
    before = len(commits)
    receipt = session.write_record(STUDY, make_record())
    assert len(commits) == before + 1
    assert commits[-1].paths == {
        "records/Study/fiber-1.json",
        "records/Study/fiber-1.md",
        f"records/Study/{INDEX_FILE}",
    }
    session.write_file(STUDY, "trace.csv", b"t,s\n")
    assert len(commits) == before + 2
    assert commits[-1].paths == {"records/Study/trace.csv", f"records/Study/{INDEX_FILE}"}
    # An identical write, with or without the receipt, makes no commit.
    session.write_record(STUDY, make_record(), prior_receipt=receipt)
    session.write_record(STUDY, make_record())
    assert len(commits) == before + 2


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


def _before_each_commit(session: GitHubSession, action) -> None:
    """Run ``action`` just before every commit the session makes: someone else
    changing the branch between MetaRelay's read and its write."""
    real_commit = session._commit

    def commit(snapshot, changes, message, where):
        action(changes)
        return real_commit(snapshot, changes, message, where)

    session._commit = commit


def _once(action):
    done = []

    def run(changes):
        if not done:
            done.append(True)
            action(changes)

    return run


def test_a_change_between_read_and_write_is_a_conflict(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record(diameter_um=1))
    theirs = make_record(diameter_um=3).model_dump_json()
    _before_each_commit(
        session, _once(lambda _: fake.put_file(REPO, "records/Study/fiber-1.json", theirs))
    )
    with pytest.raises(ConflictError, match="changed at GitHub"):
        session.write_record(STUDY, make_record(diameter_um=2), mode=WriteMode.FORCE)
    assert session.read_record(STUDY, "fiber-1").record.payload["diameter_um"] == 3


def test_create_is_enforced_by_github(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    theirs = make_record().model_dump_json()
    _before_each_commit(
        session, _once(lambda _: fake.put_file(REPO, "records/Study/fiber-1.json", theirs))
    )
    with pytest.raises(ConflictError, match="already exists"):
        session.write_record(STUDY, make_record(diameter_um=2), mode=WriteMode.CREATE)
    assert session.read_record(STUDY, "fiber-1").record.payload["diameter_um"] != 2


def test_a_concurrent_index_update_is_retried(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    session.write_record(STUDY, make_record("fiber-1"))
    git_path = f"records/Study/{INDEX_FILE}"

    def someone_lists_a_file(_):
        index = json.loads(fake.files(REPO)[git_path])
        index["files"].append("other.csv")
        fake.put_file(REPO, git_path, json.dumps(index))

    _before_each_commit(session, _once(someone_lists_a_file))
    session.write_record(STUDY, make_record("fiber-2"))
    index = json.loads(fake.files(REPO)[git_path])
    assert set(index["records"]) == {"fiber-1", "fiber-2"}
    assert index["files"] == ["other.csv"]


def test_a_commit_elsewhere_on_the_branch_is_not_a_conflict(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    first = session.write_record(STUDY, make_record())
    _before_each_commit(session, _once(lambda _: fake.put_file(REPO, "README.md", "# New\n")))
    session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)
    files = fake.files(REPO)
    assert files["README.md"] == b"# New\n"  # their commit is kept, ours is on top
    assert "200" in files["records/Study/fiber-1.md"].decode()


# Files


def test_files_over_the_cap_are_refused(
    session: GitHubSession, api_url: str, fake: FakeGitHub
) -> None:
    with pytest.raises(ValidationError, match=r"1\.0 MB"):  # the default cap
        session.write_file(STUDY, "big.bin", b"x" * (1024 * 1024 + 1))
    with closing(open_session(api_url, max_file_bytes=1024)) as capped:
        capped.write_file(STUDY, "small.csv", b"x" * 1024)
        with pytest.raises(ValidationError, match=r"1\.0 KB.*max_file_bytes"):
            capped.write_file(STUDY, "big.bin", b"x" * 1025)
    assert "records/Study/big.bin" not in fake.files(REPO)


def test_the_cap_cannot_exceed_what_github_accepts() -> None:
    with pytest.raises(ConfigurationError):
        GitHubConfig(token=TOKEN, max_file_bytes=200 * 1024 * 1024)


def test_write_file_round_trips_binary_and_receipts_the_blob_sha(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    data = bytes(range(256))
    receipt = session.write_file(STUDY, "frame.bin", data)
    assert fake.files(REPO)["records/Study/frame.bin"] == data
    assert receipt.native_id == git_blob_sha(data)


# Errors


def test_server_errors_are_backend_unavailable_without_the_token(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    fake.fail_next(502)
    with pytest.raises(BackendUnavailableError, match="server error") as caught:
        session.list_containers()
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


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
    with pytest.raises(PermissionDeniedError, match="protected"):
        session.write_record(STUDY, make_record())
    # Refused once, not retried as if it were a race.
    assert sum(method == "PATCH" for method, _ in fake.requests[before:]) == 1


def test_a_failed_upload_leaves_the_index_alone(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    fake.fail_next(500, method="PATCH")  # the last step: moving the branch
    with pytest.raises(BackendUnavailableError):
        session.write_file(STUDY, "trace.json", b"{}")
    # Nothing landed, so the name must not block a record of that name.
    session.write_record(STUDY, make_record("trace"))
    assert [item.record.record_id for item in session.list_records(STUDY)] == ["trace"]


def test_foreign_json_does_not_break_listing(api_url: str, fake: FakeGitHub, make_record) -> None:
    fake.put_file(REPO, "package.json", '{"name": "lab-site"}')
    session = open_session(api_url, "github://my-lab/metadata@main")
    assert session.list_records(ROOT) == []  # before MetaRelay ever wrote here
    session.write_record(ROOT, make_record())
    assert [item.record.record_id for item in session.list_records(ROOT)] == ["fiber-1"]
    with pytest.raises(ValidationError, match="not a valid record"):
        session.read_record(ROOT, "package")  # asked for by name: still loud
    session.close()


def test_uploaded_files_are_not_kept_in_memory(session: GitHubSession) -> None:
    session.write_file(STUDY, "trace.csv", b"x" * 1000)
    assert all(len(data) < 1000 for data in session._blobs.values())


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_a_failed_write_changes_nothing(
    session: GitHubSession, fake: FakeGitHub, make_record, method: str
) -> None:
    first = session.write_record(STUDY, make_record())
    before = fake.files(REPO)
    fake.fail_next(502, method=method)
    with pytest.raises(BackendUnavailableError):
        session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)
    assert fake.files(REPO) == before  # record, preview and index all unchanged
    # The old receipt still works: no half-written state to recover from.
    session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)


def test_a_preview_edited_mid_write_is_a_conflict(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    first = session.write_record(STUDY, make_record())
    _before_each_commit(
        session, _once(lambda _: fake.put_file(REPO, "records/Study/fiber-1.md", "mine\n"))
    )
    with pytest.raises(ConflictError, match="changed at GitHub"):
        session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)
    # Nothing of the write landed, and the edit is kept.
    assert fake.files(REPO)["records/Study/fiber-1.md"] == b"mine\n"
    assert session.read_record(STUDY, "fiber-1").receipt.checksum == first.checksum


def test_reads_come_from_one_commit(session: GitHubSession, fake: FakeGitHub, make_record) -> None:
    session.write_record(STUDY, make_record("fiber-1"))
    session.write_record(STUDY, make_record("fiber-2"))
    before = len(fake.requests)
    session.list_records(STUDY)
    assert sum("/git/ref/" in path for _, path in fake.requests[before:]) == 1


def test_an_edit_on_github_can_be_accepted_into_the_record(session, fake, make_record) -> None:
    from metarelay import ConflictError, accept_edits

    receipt = session.write_record(STUDY, make_record(diameter_um=400))
    md_path = receipt.backend_ids["path"].removesuffix(".json") + ".md"
    fake.put_file(REPO, md_path, fake.files(REPO)[md_path].decode().replace("400.0", "250"))
    with pytest.raises(ConflictError, match=r"'400\.0' → '250'") as raised:
        session.write_record(STUDY, make_record(diameter_um=400), prior_receipt=receipt)

    written = accept_edits(session, STUDY, make_record(diameter_um=400), raised.value.preview_diff)
    assert written["fiber-1"].record.payload == {"diameter_um": 250.0}
    assert "| diameter_um | 250.0 |" in fake.files(REPO)[md_path].decode()
    # The JSON and the preview agree again, so the next write goes through.
    next_receipt = written["fiber-1"].receipt
    session.write_record(STUDY, make_record(diameter_um=300), prior_receipt=next_receipt)


def test_an_edit_to_a_stale_preview_cannot_be_accepted(session, fake, make_record) -> None:
    """When the .json was changed at GitHub without its preview, the preview
    still shows the old record. Its old values must not read as a person's
    edits: accepting them would undo the change to the .json."""
    from metarelay import ConflictError, accept_edits

    first = session.write_record(STUDY, make_record(diameter_um=400))
    json_path = first.backend_ids["path"]
    md_path = json_path.removesuffix(".json") + ".md"
    fake.put_file(REPO, json_path, make_record(diameter_um=300).model_dump_json())
    text = fake.files(REPO)[md_path].decode()
    fake.put_file(REPO, md_path, text.replace("| subject_id | M1 |", "| subject_id | M2 |"))

    read = session.read_record(STUDY, "fiber-1")
    with pytest.raises(ConflictError, match="preview was edited") as raised:
        session.write_record(STUDY, make_record(diameter_um=300), prior_receipt=read.receipt)
    diff = raised.value.preview_diff
    assert diff is not None and not diff.complete
    assert diff.edits == []
    assert "diameter_um" not in str(raised.value)
    with pytest.raises(ConflictError, match="can't list"):
        accept_edits(session, STUDY, make_record(diameter_um=300), diff)
    assert session.read_record(STUDY, "fiber-1").record.payload == {"diameter_um": 300.0}


# Found in review, checked against real GitHub


RACE = "Update is not a fast forward"  # GitHub's answer when the branch moved


# Both seen live: the second when several writers move the branch at once.
@pytest.mark.parametrize("message", [RACE, "Reference cannot be updated"])
def test_a_commit_racing_on_the_branch_is_retried(
    session: GitHubSession, fake: FakeGitHub, make_record, monkeypatch, message: str
) -> None:
    monkeypatch.setattr(github_backend, "_BRANCH_BACKOFF", 0)
    for _ in range(3):
        fake.fail_next(422, message, method="PATCH")
    session.write_record(STUDY, make_record())
    assert [item.record.record_id for item in session.list_records(STUDY)] == ["fiber-1"]
    assert "records/Study/fiber-1.md" in fake.files(REPO)


def test_a_branch_that_keeps_moving_is_backend_unavailable(
    session: GitHubSession, fake: FakeGitHub, make_record, monkeypatch
) -> None:
    monkeypatch.setattr(github_backend, "_BRANCH_BACKOFF", 0)
    for _ in range(github_backend._BRANCH_RETRIES):
        fake.fail_next(422, RACE, method="PATCH")
    with pytest.raises(BackendUnavailableError, match="Other commits kept landing") as raised:
        session.write_record(STUDY, make_record())
    assert type(raised.value) is BackendUnavailableError  # the public class
    assert "records/Study/fiber-1.json" not in fake.files(REPO)


def test_big_folders_are_read_whole(session: GitHubSession, fake: FakeGitHub, make_record) -> None:
    receipt = session.write_record(STUDY, make_record("zz-last"))  # sorts last
    for number in range(1100):  # more than the Contents API would list
        fake.put_file(REPO, f"records/Study/a{number:04}.txt", "x")
    assert [item.record.record_id for item in session.list_records(STUDY)] == ["zz-last"]
    session.write_record(STUDY, make_record("zz-last", 250.0), prior_receipt=receipt)
    assert session.read_record(STUDY, "zz-last").record.payload["diameter_um"] == 250.0


NFD = "Expe\u0301rience"  # committed by some Linux tools
NFC = "Exp\u00e9rience"


def test_a_folder_committed_in_nfd_is_refused_not_duplicated(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    fake.put_file(REPO, f"records/{NFD}/notes.txt", "x")
    with pytest.raises(ValidationError, match="Unicode form"):
        session.ensure_container(LogicalPath((NFC, "Cohort A")))
    with pytest.raises(ValidationError, match="Unicode form"):
        session.list_records(LogicalPath((NFC,)))
    assert not any(path.startswith(f"records/{NFC}/") for path in fake.files(REPO))


def test_a_file_committed_in_nfd_is_refused_not_duplicated(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    fake.put_file(REPO, f"records/Study/{NFD}.txt", "x")
    with pytest.raises(ValidationError, match="Unicode form"):
        session.write_file(STUDY, f"{NFC}.txt", b"y")
    assert f"records/Study/{NFC}.txt" not in fake.files(REPO)


def test_an_upload_racing_on_the_same_name_is_a_conflict(
    session: GitHubSession, fake: FakeGitHub
) -> None:
    git_path = "records/Study/calib.json"
    _before_each_commit(session, _once(lambda _: fake.put_file(REPO, git_path, b"{}")))
    with pytest.raises(ConflictError, match="already exists"):
        session.write_file(STUDY, "calib.json", b'{"x": 1}')
    assert fake.files(REPO)[git_path] == b"{}"  # theirs, untouched


# Found in Johnny's review


def test_write_file_updates_a_json_file_committed_by_hand(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    fake.put_file(REPO, "records/Study/config.json", '{"rate": 1}')
    session.write_file(STUDY, "config.json", b'{"rate": 2}')
    assert fake.files(REPO)["records/Study/config.json"] == b'{"rate": 2}'
    assert session.list_records(STUDY) == []


def test_write_file_still_refuses_a_hand_committed_record(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    fake.put_file(REPO, "records/Study/fiber-1.json", make_record().model_dump_json())
    with pytest.raises(ValidationError, match="record's file name"):
        session.write_file(STUDY, "fiber-1.json", b"{}")
    with pytest.raises(ValidationError, match="record's file name"):
        session.write_file(STUDY, "fiber-1.md", b"notes")


@pytest.mark.parametrize("prefix", ["CON", "a%3Ab", "records/trailing.", "aux.txt"])
def test_prefixes_that_break_a_checkout_are_refused(api_url: str, prefix: str) -> None:
    with pytest.raises(ConfigurationError, match="prefix"):
        open_session(api_url, f"github://my-lab/metadata@main/{prefix}")


def test_a_lagging_branch_read_does_not_hide_our_own_commit(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    # Seen live: right after a commit, GitHub can still report the branch at
    # the commit before it.
    before = fake.repo(REPO).branches["main"]
    session.ensure_container(LogicalPath(("Study", "Cohort A")))
    real_request = session._request

    def stale_ref(method, url, action, **kwargs):
        if method == "GET" and "/git/ref/" in url:
            return {"object": {"sha": before}}
        return real_request(method, url, action, **kwargs)

    session._request = stale_ref
    session.write_record(LogicalPath(("Study", "Cohort A")), make_record())
    session._request = real_request
    assert "records/Study/Cohort A/fiber-1.json" in fake.files(REPO)


def test_a_branch_reset_to_undo_our_write_is_respected(
    session: GitHubSession, fake: FakeGitHub, make_record, monkeypatch
) -> None:
    before = fake.repo(REPO).branches["main"]
    session.write_record(STUDY, make_record())
    monkeypatch.setattr(github_backend, "_REPLICA_LAG", 0)  # the lag window has passed
    fake.repo(REPO).branches["main"] = before  # someone resets the branch
    assert session.list_records(STUDY) == []
    session.write_record(STUDY, make_record("fiber-2"))
    assert "records/Study/fiber-1.json" not in fake.files(REPO)  # not brought back


def test_a_prefix_differing_only_in_case_is_refused(api_url: str, fake: FakeGitHub) -> None:
    fake.put_file(REPO, "records/Study/.metarelay.json", "{}")
    uri = "github://my-lab/metadata@main/Records"
    with (
        closing(open_session(api_url, uri)) as session,
        pytest.raises(ValidationError, match="differs only in case"),
    ):
        session.ensure_container(STUDY)
    assert not any(path.startswith("Records/") for path in fake.files(REPO))


def test_a_prefix_committed_in_nfd_is_refused(api_url: str, fake: FakeGitHub) -> None:
    fake.put_file(REPO, f"{NFD}/notes.txt", "x")
    uri = f"github://my-lab/metadata@main/{NFC}"
    with (
        closing(open_session(api_url, uri)) as session,
        pytest.raises(ValidationError, match="Unicode form"),
    ):
        session.list_containers()


def test_a_dot_prefix_is_allowed(api_url: str, fake: FakeGitHub) -> None:
    with closing(open_session(api_url, "github://my-lab/metadata@main/.metadata")) as session:
        session.ensure_container(STUDY)
    assert f".metadata/Study/{INDEX_FILE}" in fake.files(REPO)


def test_write_file_and_list_records_agree_on_a_misnamed_record(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    fake.put_file(REPO, "records/Study/foo.json", make_record("bar").model_dump_json())
    assert session.list_records(STUDY) == []
    session.write_file(STUDY, "foo.json", b"{}")  # not a record, so a file


def test_an_identical_write_racing_ours_is_not_a_conflict(
    session: GitHubSession, api_url: str, make_record
) -> None:
    record = make_record()
    # A rerun of the same pipeline commits the same record first.
    with closing(open_session(api_url)) as other:
        _before_each_commit(session, _once(lambda _: other.write_record(STUDY, record)))
        session.write_record(STUDY, record)
    assert session.read_record(STUDY, "fiber-1").record == record


def test_a_branch_deleted_during_a_write_is_not_found(
    session: GitHubSession, fake: FakeGitHub, make_record
) -> None:
    _before_each_commit(session, _once(lambda _: fake.repo(REPO).branches.pop("main")))
    with pytest.raises(NotFoundError, match="no branch 'main' any more"):
        session.write_record(STUDY, make_record())
