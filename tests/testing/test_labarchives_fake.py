"""The fake LabArchives server, checked with the real ``labapi`` client.

The fake is only useful if ``labapi`` can't tell it from LabArchives, so most
of these tests drive it through ``labapi`` exactly as the backend will.
"""

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


def test_signature_check_can_be_turned_off(fake: FakeLabArchives) -> None:
    fake.verify_signatures = False
    with serve_fake(fake) as url:
        client = labapi.Client(url, "anything", "anything")
        assert client.login("ada@example.org", "code-1").id


def test_login_page_redirects_back_with_email_and_code_when_signed(client, base_url: str) -> None:
    auth_url = client.generate_auth_url("http://localhost:8501/callback?x=1")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _open_without_redirects(auth_url)
    assert caught.value.code == 302
    location = urlsplit(caught.value.headers["Location"])
    params = parse_qs(location.query)
    assert location.path == "/callback"
    assert params == {"x": ["1"], "email": ["ada@example.org"], "auth_code": ["code-1"]}

    unsigned = labapi.Client(base_url, AKID, "wrong-password")
    auth_url = unsigned.generate_auth_url("http://localhost:8501/callback")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _open_without_redirects(auth_url)
    assert caught.value.code == 401
    unsigned.close()


# The notebook tree


def test_tree_is_per_notebook(fake: FakeLabArchives, user) -> None:
    fake.add_notebook("ada@example.org", "Other")
    user = fresh(user)
    user.notebooks["Lab Notebook"].create(labapi.NotebookDirectory, "Only here")
    assert fresh(user).notebooks["Other"].children == ()


# Entries


def test_names_and_text_entries_round_trip_exactly_and_escaped(user) -> None:
    name = "Cohort ä & <b>"
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, name)
    html = "<!-- marker --><p>a &amp; b</p>\n<table><tr><td>ä</td></tr></table>"
    entry = page.entries.create(labapi.TextEntry, html)
    assert entry.id

    read_page = fresh(user).notebooks["Lab Notebook"][name]
    assert read_page.name == name
    [read] = read_page.entries
    assert isinstance(read, labapi.TextEntry)
    assert read.content == html


# Shapes captured from real LabArchives (tests/cassettes/labarchives, issue #27)


def test_adding_and_updating_entries_and_attachments_answer_with_the_entry_inside_entries(
    user,
) -> None:
    page = user.notebooks["Lab Notebook"].create(labapi.NotebookPage, "Page")
    nbid = user.notebooks["Lab Notebook"].id
    added = user.api_post(
        "entries/add_entry",
        {"entry_data": "<p>a</p>"},
        part_type="text entry",
        pid=page.id,
        nbid=nbid,
    )
    eid = added.findtext("entry/eid")
    updated = user.api_post("entries/update_entry", {"entry_data": "<p>b</p>"}, eid=eid)

    for answer in (added, updated):
        assert answer.tag == "entries"
        assert answer.findtext("entry/eid") == eid
        # LabArchives sends the text back only when a page's entries are listed.
        assert answer.find("entry/entry-data") is None
    assert updated.findtext("entry/version") == "2"

    added = user.api_post(
        "entries/add_attachment",
        BytesIO(b"old"),
        filename="a.txt",
        caption="c",
        pid=page.id,
        nbid=nbid,
    )
    eid = added.findtext("entry/eid")
    updated = user.api_post(
        "entries/update_attachment", BytesIO(b"new"), filename="a.txt", caption="c", eid=eid
    )
    for answer in (added, updated):
        assert answer.tag == "entries"
        assert answer.findtext("entry/eid") == eid
        assert answer.find("entry/entry-data") is None


def test_an_id_that_names_nothing_is_error_4999_inside_the_class_root(
    fake: FakeLabArchives, user
) -> None:
    with pytest.raises(labapi.ApiError) as caught:
        user.api_get(
            "tree_tools/get_tree_level",
            nbid=user.notebooks["Lab Notebook"].id,
            parent_tree_id="999999999",
        )
    assert caught.value.error_code == 4999

    fake.verify_signatures = False
    status, _, body = fake.handle(
        "GET",
        "/api/tree_tools/get_tree_level",
        {"uid": user.id, "nbid": user.notebooks["Lab Notebook"].id, "parent_tree_id": "999999999"},
        b"",
        "",
    )
    assert status == 400
    assert body.decode() == (
        '<tree-tools><error><error-code type="integer">4999</error-code>'
        "<error-description>your request contained bad information</error-description>"
        "</error></tree-tools>"
    )


def test_other_users_cannot_see_a_notebook(fake: FakeLabArchives, client, user) -> None:
    fake.add_user("bob@example.org", auth_code="code-2", notebooks=["Bob's"])
    bob = client.login("bob@example.org", "code-2")
    ada_notebook = user.notebooks["Lab Notebook"]
    with pytest.raises(labapi.ApiError):
        bob.api_get("tree_tools/get_tree_level", nbid=ada_notebook.id, parent_tree_id="0")


def _open_without_redirects(url: str) -> None:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    urllib.request.build_opener(NoRedirect).open(url)
