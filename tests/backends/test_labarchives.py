"""The LabArchives backend, run against the fake LabArchives server.

The real ``labapi`` client talks HTTP to ``FakeLabArchives``, so these tests
exercise the same code path as production. DSST's live run (R1) is the check
that the fake is right.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlsplit

import pytest

from metarelay import (
    ROOT,
    AuthenticationError,
    BackendUnavailableError,
    ConfigurationError,
    ConflictError,
    LogicalPath,
    ValidationError,
    WriteMode,
    auth_flow,
    begin_login,
    complete_login,
    connect,
)
from metarelay.core.auth import AuthFlow
from metarelay.testing import BackendConformance
from metarelay.testing.labarchives_fake import FakeLabArchives, serve_fake

pytest.importorskip("labapi")

from metarelay.backends.labarchives import (
    FILES_PAGE,
    LabArchivesConfig,
    LabArchivesSession,
)

AKID, AKPASS = "test-akid", "test-akpass"
EMAIL, CODE = "ada@example.org", "code-1"
CALLBACK = "http://localhost:8501/callback"
NOTEBOOK = "Lab Notebook"
STUDY = LogicalPath((NOTEBOOK, "Study"))


def follow_login(auth_url: str) -> dict[str, str]:
    """Visit the login page as a browser would; return the callback's query."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.build_opener(NoRedirect).open(auth_url)
    return dict(parse_qsl(urlsplit(caught.value.headers["Location"]).query))


def uri_for(base_url: str) -> str:
    return "labarchives://" + urlsplit(base_url).netloc


def config_for(base_url: str, **overrides) -> LabArchivesConfig:
    return LabArchivesConfig(**{"akid": AKID, "akpass": AKPASS, "api_url": base_url, **overrides})


def login(base_url: str, **overrides) -> LabArchivesSession:
    config = config_for(base_url, **overrides)
    challenge = begin_login(uri_for(base_url), CALLBACK, config)
    return complete_login(challenge, follow_login(challenge.auth_url), config)


@pytest.fixture
def fake() -> FakeLabArchives:
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user(EMAIL, auth_code=CODE, notebooks=[NOTEBOOK])
    return fake


@pytest.fixture
def base_url(fake: FakeLabArchives):
    with serve_fake(fake) as url:
        yield url


class TestLabArchivesBackend(BackendConformance):
    @pytest.fixture
    def session(self, fake: FakeLabArchives, base_url: str):
        self.fake = fake
        session = login(base_url)
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        receipt = session.read_record(path, record_id).receipt
        self.fake.edit_entry(receipt.backend_ids["preview_entry_id"], text)


@pytest.fixture
def session(base_url: str) -> LabArchivesSession:
    session = login(base_url)
    session.ensure_container(STUDY)
    yield session
    session.close()


def records_page(fake: FakeLabArchives, receipt) -> list[dict]:
    return fake.entries_on(receipt.backend_ids["page_id"])


# Connecting and logging in


def test_uses_the_redirect_flow(base_url: str) -> None:
    assert auth_flow(uri_for(base_url)) is AuthFlow.REDIRECT
    with pytest.raises(ConfigurationError, match="browser login"):
        connect(uri_for(base_url))


def test_login_challenge_points_at_the_login_page(base_url: str) -> None:
    challenge = begin_login(uri_for(base_url), CALLBACK, config_for(base_url))
    assert challenge.auth_url.startswith(f"{base_url}/api_user_login?")
    assert challenge.redirect_url == CALLBACK
    assert challenge.destination == uri_for(base_url)


def test_session_destination_is_the_uri(session: LabArchivesSession, base_url: str) -> None:
    assert session.destination == uri_for(base_url)


def test_default_api_url_is_https_on_the_host() -> None:
    config = LabArchivesConfig(akid=AKID, akpass=AKPASS)
    challenge = begin_login("labarchives://api.labarchives.com", CALLBACK, config)
    assert challenge.auth_url.startswith("https://api.labarchives.com/api_user_login?")


def test_config_reads_its_own_environment_variables(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
    monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", base_url)
    challenge = begin_login(uri_for(base_url), CALLBACK)
    session = complete_login(challenge, follow_login(challenge.auth_url))
    assert session.list_containers(ROOT)


def test_missing_credentials_are_a_configuration_error(monkeypatch) -> None:
    for name in ("AKID", "AKPASS", "API_URL"):
        monkeypatch.delenv(f"METARELAY_LABARCHIVES_{name}", raising=False)
    with pytest.raises(ConfigurationError):
        begin_login("labarchives://api.labarchives.com", CALLBACK)


def test_akpass_is_never_printed() -> None:
    config = LabArchivesConfig(akid=AKID, akpass="very-secret")
    assert "very-secret" not in repr(config)


@pytest.mark.parametrize(
    "uri", ["labarchives://host/notebook", "labarchives://host?x=1", "labarchives://"]
)
def test_rejects_paths_queries_and_missing_hosts(uri: str) -> None:
    with pytest.raises(ConfigurationError):
        begin_login(uri, CALLBACK, LabArchivesConfig(akid=AKID, akpass=AKPASS))


def test_rejects_a_non_http_api_url() -> None:
    with pytest.raises(ConfigurationError):
        LabArchivesConfig(akid=AKID, akpass=AKPASS, api_url="ftp://host")


@pytest.mark.parametrize(
    "params",
    [
        {"error": "access denied"},
        {"email": EMAIL},
        {"auth_code": CODE},
        {"email": EMAIL, "auth_code": "wrong"},
    ],
)
def test_failed_login_is_an_authentication_error(base_url: str, params) -> None:
    config = config_for(base_url)
    challenge = begin_login(uri_for(base_url), CALLBACK, config)
    with pytest.raises(AuthenticationError):
        complete_login(challenge, params, config)


def test_wrong_api_password_is_an_authentication_error(base_url: str) -> None:
    with pytest.raises(AuthenticationError):
        login(base_url, akpass="wrong")


def test_unreachable_server_is_unavailable_and_leaks_no_signature(fake: FakeLabArchives) -> None:
    with serve_fake(fake) as url:
        session = login(url)
        session.ensure_container(STUDY)
    # The server is gone; the session still points at it.
    with pytest.raises(BackendUnavailableError) as caught:
        session.list_containers(STUDY)
    assert "sig=" not in str(caught.value)
    assert AKPASS not in str(caught.value)


# How a LogicalPath maps onto the notebook


def test_root_lists_notebooks(fake: FakeLabArchives, base_url: str) -> None:
    fake.add_notebook(EMAIL, "Second")
    session = login(base_url)
    names = {container.name for container in session.list_containers(ROOT)}
    assert names == {NOTEBOOK, "Second"}
    assert all(container.native_id for container in session.list_containers(ROOT))


def test_folders_are_containers_and_pages_are_not(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    session.write_file(STUDY, "trace.csv", b"data")
    nbid = receipt.backend_ids["notebook_id"]
    study_id = session.list_containers(LogicalPath((NOTEBOOK,)))[0].native_id
    fake.add_node(nbid, study_id, "Someone's page", is_page=True)

    assert session.list_containers(STUDY) == []


def test_records_live_on_a_page_in_the_folder(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    record = make_record()
    receipt = session.write_record(STUDY, record)

    [attachment, preview] = records_page(fake, receipt)
    assert attachment["part_type"] == "Attachment"
    assert attachment["data"] == "MetaRelay record | fiber-1"
    assert attachment["filename"] == "fiber-1.json"
    assert json.loads(attachment["content"])["record_id"] == "fiber-1"
    assert preview["part_type"] == "text entry"
    assert preview["data"].startswith("<!-- metarelay-record:fiber-1 -->")
    assert "<table" in preview["data"]
    assert receipt.backend_ids == {
        "notebook_id": receipt.backend_ids["notebook_id"],
        "page_id": receipt.backend_ids["page_id"],
        "attachment_entry_id": attachment["eid"],
        "preview_entry_id": preview["eid"],
    }


def test_ensuring_a_container_creates_no_pages(
    session: LabArchivesSession, fake: FakeLabArchives
) -> None:
    session.ensure_container(STUDY / "Cohort A")
    assert ("entries", "add_entry") not in fake.calls
    assert ("tree_tools", "insert_node") in fake.calls
    assert not any(node.is_page for node in fake._nodes.values())


def test_ensure_container_creates_a_missing_notebook(session: LabArchivesSession) -> None:
    session.ensure_container(LogicalPath(("New notebook", "Study")))
    assert "New notebook" in {c.name for c in session.list_containers(ROOT)}


def test_names_that_look_like_path_escapes(session: LabArchivesSession, make_record) -> None:
    path = LogicalPath((NOTEBOOK, r"a\b", "c\\"))
    session.ensure_container(path)
    assert {c.name for c in session.list_containers(LogicalPath((NOTEBOOK,)))} >= {r"a\b"}
    record = make_record()
    session.write_record(path, record)
    assert session.read_record(path, "fiber-1").record == record


def test_two_folders_with_the_same_name_are_ambiguous(
    session: LabArchivesSession, fake: FakeLabArchives
) -> None:
    notebook = session.list_containers(ROOT)[0]
    fake.add_node(notebook.native_id, "0", "Study", is_page=False)
    with pytest.raises(ConflictError, match="Study"):
        session.list_containers(STUDY)


def test_records_at_the_root_are_refused(session: LabArchivesSession, make_record) -> None:
    with pytest.raises(ValidationError, match="notebook"):
        session.write_record(ROOT, make_record())
    assert session.list_records(ROOT) == []


# Records next to other people's entries


def test_other_entries_on_the_page_are_left_alone(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    page_id = receipt.backend_ids["page_id"]
    fake.add_text_entry(page_id, "<p>Lab notes</p>")
    fake.add_attachment(page_id, "photo.png", b"png", "A photo")

    session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=receipt)

    assert [entry["data"] for entry in fake.entries_on(page_id)][2:] == [
        "<p>Lab notes</p>",
        "A photo",
    ]
    assert [stored.record.record_id for stored in session.list_records(STUDY)] == ["fiber-1"]


def test_an_update_replaces_entries_in_place(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    first = session.write_record(STUDY, make_record())
    second = session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=first)
    assert second.backend_ids == first.backend_ids
    assert len(records_page(fake, second)) == 2


def test_attachment_replaced_by_hand_conflicts(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    theirs = make_record(diameter_um=999.0).model_dump_json().encode()
    fake.replace_attachment(receipt.backend_ids["attachment_entry_id"], theirs)
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=receipt)


def test_reads_are_never_stale(base_url: str, make_record) -> None:
    mine, theirs = login(base_url), login(base_url)
    mine.ensure_container(STUDY)
    mine.write_record(STUDY, make_record())
    assert theirs.read_record(STUDY, "fiber-1").record.payload["diameter_um"] == 400.0
    mine.write_record(STUDY, make_record(diameter_um=200.0), mode=WriteMode.FORCE)
    assert theirs.read_record(STUDY, "fiber-1").record.payload["diameter_um"] == 200.0
    theirs.ensure_container(STUDY / "New")
    assert {c.name for c in mine.list_containers(STUDY)} == {"New"}


def test_damaged_record_conflicts_and_force_repairs_it(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    fake.replace_attachment(receipt.backend_ids["attachment_entry_id"], b"{not json")

    with pytest.raises(ValidationError, match="not a valid record"):
        session.read_record(STUDY, "fiber-1")
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(), prior_receipt=receipt)
    session.write_record(STUDY, make_record(), mode=WriteMode.FORCE)
    assert session.read_record(STUDY, "fiber-1").record.payload == {"diameter_um": 400.0}


def test_missing_preview_is_restored(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    fake.delete_entry(receipt.backend_ids["preview_entry_id"])
    again = session.write_record(STUDY, make_record(), prior_receipt=receipt)
    assert again.backend_ids["preview_entry_id"] != receipt.backend_ids["preview_entry_id"]
    assert len(records_page(fake, again)) == 2


def test_duplicate_record_entries_are_a_conflict(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    data = make_record().model_dump_json().encode()
    fake.add_attachment(
        receipt.backend_ids["page_id"], "fiber-1.json", data, "MetaRelay record | fiber-1"
    )
    with pytest.raises(ConflictError, match="more than one"):
        session.read_record(STUDY, "fiber-1")


# Files


def test_files_go_on_their_own_page(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_file(STUDY, "trace.csv", b"t,s\n", media_type="text/csv")
    assert receipt.native_id
    record_receipt = session.write_record(STUDY, make_record())
    assert len(records_page(fake, record_receipt)) == 2  # the file is not on this page

    files_page = next(
        node for node in fake._nodes.values() if node.is_page and node.name == FILES_PAGE
    )
    [entry] = fake.entries_on(files_page.tree_id)
    assert entry["data"] == "MetaRelay file | trace.csv"
    assert entry["filename"] == "trace.csv"
    assert entry["content"] == b"t,s\n"


@pytest.mark.parametrize("name", ["", "a/b", "line\nbreak"])
def test_bad_file_names_are_refused(session: LabArchivesSession, name: str) -> None:
    with pytest.raises(ValidationError):
        session.write_file(STUDY, name, b"data")


def test_preview_edit_that_keeps_the_marker_conflicts(
    session: LabArchivesSession, fake: FakeLabArchives, make_record
) -> None:
    receipt = session.write_record(STUDY, make_record())
    eid = receipt.backend_ids["preview_entry_id"]
    edited = next(e["data"] for e in records_page(fake, receipt) if e["eid"] == eid) + "<p>!</p>"
    fake.edit_entry(eid, edited)
    with pytest.raises(ConflictError, match="edited directly"):
        session.write_record(STUDY, make_record(diameter_um=200.0), prior_receipt=receipt)
