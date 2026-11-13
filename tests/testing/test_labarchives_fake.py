"""The fake LabArchives server, checked with the real ``labapi`` client.

The fake is only useful if ``labapi`` can't tell it from LabArchives, so most
of these tests drive it through ``labapi`` exactly as the backend will.
"""

import json
import urllib.error
import urllib.request
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest

from metarelay.testing.labarchives_fake import FakeLabArchives, serve_fake

labapi = pytest.importorskip("labapi")

AKID, AKPASS = "test-akid", "test-akpass"


@pytest.fixture
def fake() -> FakeLabArchives:
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user("ada@example.org", auth_code="code-1", notebooks=["Lab Notebook"])
    return fake


@pytest.fixture
def base_url(fake: FakeLabArchives):
    with serve_fake(fake) as url:
        yield url


@pytest.fixture
def client(base_url: str):
    client = labapi.Client(base_url, AKID, AKPASS)
    yield client
    client.close()


@pytest.fixture
def user(client):
    return client.login("ada@example.org", "code-1")


def fresh(user):
    """A second login, so nothing comes from labapi's cache."""
    return user.client.login("ada@example.org", "code-1")


# Auth


def test_login_returns_the_users_notebooks(user) -> None:
    assert [notebook.name for notebook in user.notebooks.all_values()] == ["Lab Notebook"]


def test_wrong_auth_code_is_an_authentication_error(client) -> None:
    with pytest.raises(labapi.AuthenticationError):
        client.login("ada@example.org", "wrong")


def test_bad_signature_is_an_authentication_error(base_url: str) -> None:
    client = labapi.Client(base_url, AKID, "wrong-password")
    with pytest.raises(labapi.AuthenticationError):
        client.login("ada@example.org", "code-1")


def test_signature_check_can_be_turned_off(fake: FakeLabArchives) -> None:
    fake.verify_signatures = False
    with serve_fake(fake) as url:
        client = labapi.Client(url, "anything", "anything")
        assert client.login("ada@example.org", "code-1").id


def test_login_page_redirects_back_with_email_and_code(client) -> None:
    auth_url = client.generate_auth_url("http://localhost:8501/callback?x=1")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _open_without_redirects(auth_url)
    assert caught.value.code == 302
    location = urlsplit(caught.value.headers["Location"])
    params = parse_qs(location.query)
    assert location.path == "/callback"
    assert params == {"x": ["1"], "email": ["ada@example.org"], "auth_code": ["code-1"]}


def test_login_page_checks_the_signature(base_url: str) -> None:
    client = labapi.Client(base_url, AKID, "wrong-password")
    auth_url = client.generate_auth_url("http://localhost:8501/callback")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _open_without_redirects(auth_url)
    assert caught.value.code == 401


# The notebook tree


def test_create_and_browse_folders_and_pages(user) -> None:
    notebook = user.notebooks["Lab Notebook"]
    study = notebook.create(labapi.NotebookDirectory, "Study")
    study.create(labapi.NotebookPage, "Equipment")
    study.create(labapi.NotebookDirectory, "Cohort A")

    notebook = fresh(user).notebooks["Lab Notebook"]
    children = notebook["Study"].children
    assert [(child.name, child.is_dir()) for child in children] == [
        ("Equipment", False),
        ("Cohort A", True),
    ]


def test_create_notebook(user) -> None:
    user.notebooks.create_notebook("Second")
    assert "Second" in fresh(user).notebooks.all_keys()


def test_tree_is_per_notebook(fake: FakeLabArchives, user) -> None:
    fake.add_notebook("ada@example.org", "Other")
    user = fresh(user)
    user.notebooks["Lab Notebook"].create(labapi.NotebookDirectory, "Only here")
    assert fresh(user).notebooks["Other"].children == ()


def test_rename_and_move(user) -> None:
    notebook = user.notebooks["Lab Notebook"]
    first = notebook.create(labapi.NotebookDirectory, "First")
    page = notebook.create(labapi.NotebookPage, "Page")
    page.name = "Renamed"
    page.move_to(first)

    notebook = fresh(user).notebooks["Lab Notebook"]
    assert [child.name for child in notebook.children] == ["First"]
    assert [child.name for child in notebook["First"].children] == ["Renamed"]


def test_names_round_trip(user) -> None:
    name = "Cohort ä & <b>"
    user.notebooks["Lab Notebook"].create(labapi.NotebookDirectory, name)
    assert fresh(user).notebooks["Lab Notebook"][name].name == name


# Entries


def test_text_entries_round_trip_exactly(user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    html = "<!-- marker --><p>a &amp; b</p>\n<table><tr><td>ä</td></tr></table>"
    entry = page.entries.create(labapi.TextEntry, html)
    assert entry.id

    [read] = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert isinstance(read, labapi.TextEntry)
    assert read.content == html


def test_update_text_entry(user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    entry = page.entries.create(labapi.TextEntry, "<p>old</p>")
    entry.content = "<p>new</p>"
    [read] = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert read.content == "<p>new</p>"


def test_attachment_round_trip(user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    data = json.dumps({"a": 1}).encode()
    page.entries.create(
        labapi.AttachmentEntry,
        labapi.Attachment(BytesIO(data), "application/json", "rec.json", "my caption"),
    )

    [read] = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert isinstance(read, labapi.AttachmentEntry)
    assert read.caption == "my caption"
    attachment = read.content
    assert attachment.read() == data
    assert attachment.filename == "rec.json"
    assert attachment.mime_type == "application/json"


def test_update_attachment(user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    entry = page.entries.create(
        labapi.AttachmentEntry,
        labapi.Attachment(BytesIO(b"old"), "text/plain", "a.txt", "first"),
    )
    entry.content = labapi.Attachment(BytesIO(b"new"), "text/plain", "b.txt", "second")

    [read] = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert read.caption == "second"
    assert read.content.read() == b"new"
    assert read.content.filename == "b.txt"


def test_create_json_entry(user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    page.entries.create_json_entry({"a": 1}, filename="a.json", caption="cap")
    entries = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert [type(entry).__name__ for entry in entries] == ["AttachmentEntry", "TextEntry"]


def test_unknown_page_is_an_api_error(user) -> None:
    with pytest.raises(labapi.ApiError):
        user.api_get("tree_tools/get_entries_for_page", page_tree_id="nope", nbid="nope")


def test_other_users_cannot_see_a_notebook(fake: FakeLabArchives, client, user) -> None:
    fake.add_user("bob@example.org", auth_code="code-2", notebooks=["Bob's"])
    bob = client.login("bob@example.org", "code-2")
    ada_notebook = user.notebooks["Lab Notebook"]
    with pytest.raises(labapi.ApiError):
        bob.api_get("tree_tools/get_tree_level", nbid=ada_notebook.id, parent_tree_id="0")


# Test hooks and fixtures


def test_edit_entry_hook_changes_what_labapi_sees(fake: FakeLabArchives, user) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    entry = page.entries.create(labapi.TextEntry, "<p>mine</p>")
    fake.edit_entry(entry.id, "<p>edited by hand</p>")
    [read] = fresh(user).notebooks["Lab Notebook"]["Page"].entries
    assert read.content == "<p>edited by hand</p>"


def test_seed_from_a_fixture_file(tmp_path) -> None:
    seed = {
        "users": [
            {
                "email": "ada@example.org",
                "auth_code": "code-1",
                "notebooks": [
                    {
                        "name": "Lab Notebook",
                        "tree": [
                            {"folder": "Study", "children": [{"page": "Equipment"}]},
                        ],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed))
    fake = FakeLabArchives.from_seed(path, akid=AKID, akpass=AKPASS)
    with serve_fake(fake) as url:
        client = labapi.Client(url, AKID, AKPASS)
        user = client.login("ada@example.org", "code-1")
        study = user.notebooks["Lab Notebook"]["Study"]
        assert [child.name for child in study.children] == ["Equipment"]


def test_records_requests_for_inspection(fake: FakeLabArchives, user) -> None:
    user.notebooks["Lab Notebook"].children  # noqa: B018
    assert ("users", "user_access_info") in fake.calls
    assert ("tree_tools", "get_tree_level") in fake.calls


def _open_without_redirects(url: str) -> None:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    urllib.request.build_opener(NoRedirect).open(url)
