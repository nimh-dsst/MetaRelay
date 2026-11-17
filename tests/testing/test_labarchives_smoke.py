"""The live smoke script and the cassette harness, run against the fake.

DSST runs the smoke script against real LabArchives with ``--record`` (R1).
Running it against the fake here keeps the script working, and comparing a
captured cassette with the fake's answers shows where the fake is wrong.
"""

import json
from pathlib import Path

import pytest

from metarelay.testing.labarchives_fake import FakeLabArchives, serve_fake

pytest.importorskip("labapi")

from metarelay.testing.labarchives_cassettes import (
    _redact_text,
    anonymize,
    compare_error_codes,
    compare_shapes,
    error_codes,
    leaks,
    load_cassette,
    xml_shapes,
)
from metarelay.testing.labarchives_cassettes import main as cassettes_main
from metarelay.testing.labarchives_smoke import MISSING_TREE_ID, main

AKID, AKPASS = "smoke-akid", "smoke-akpass-secret"
EMAIL, CODE = "ada@example.org", "auth-code-secret"
REPO = Path(__file__).resolve().parents[2]
CASSETTES = sorted((REPO / "tests" / "cassettes" / "labarchives").glob("*.json"))


@pytest.fixture
def base_url(monkeypatch):
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user(EMAIL, auth_code=CODE, notebooks=["Lab Notebook"])
    with serve_fake(fake) as url:
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
        monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", url)
        yield url


def run_smoke(base_url: str, *extra: str) -> int:
    uri = "labarchives://" + base_url.removeprefix("http://")
    return main(["--uri", uri, "--notebook", "Lab Notebook", "--auto-login", *extra])


def fake_cassette(tmp_path: Path) -> Path:
    """Record the smoke run against a fresh fake."""
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user(EMAIL, auth_code=CODE, notebooks=["Lab Notebook"])
    path = tmp_path / "fake.json"
    with serve_fake(fake) as url, pytest.MonkeyPatch.context() as patch:
        patch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
        patch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
        patch.setenv("METARELAY_LABARCHIVES_API_URL", url)
        assert run_smoke(url, "--record", str(path)) == 0
    return path


# The smoke script


def test_smoke_passes_against_the_fake(base_url: str, capsys) -> None:
    assert run_smoke(base_url) == 0
    out = capsys.readouterr().out
    assert "All smoke checks passed" in out
    assert "conflict" in out
    assert "LabArchives keeps HTML comments" in out
    assert "updated in place" in out
    assert "can't contain folders" in out
    assert "missing folder with error code 4999" in out


def test_smoke_reports_stripped_comments_and_still_passes(monkeypatch, capsys) -> None:
    # If LabArchives drops HTML comments when it saves an entry, the caption
    # still finds the preview; the smoke run says so instead of failing.
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user(EMAIL, auth_code=CODE, notebooks=["Lab Notebook"])
    fake.strip_html_comments = True
    with serve_fake(fake) as url:
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
        monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", url)
        assert run_smoke(url) == 0
    out = capsys.readouterr().out
    assert "removes HTML comments" in out
    assert "All smoke checks passed" in out


def test_check_edits_catches_an_edit_made_by_hand(monkeypatch, capsys) -> None:
    fake = FakeLabArchives(akid=AKID, akpass=AKPASS)
    fake.add_user(EMAIL, auth_code=CODE, notebooks=["Lab Notebook"])

    def edit_the_preview(prompt: str = "") -> str:
        [entry] = [
            entry
            for entry in fake._entries.values()
            if entry.data.startswith("<!-- metarelay-record:smoke-1 -->")
        ]
        fake.edit_entry(entry.eid, entry.data.replace("second", "changed by hand"))
        return ""

    monkeypatch.setattr("builtins.input", edit_the_preview)
    with serve_fake(fake) as url:
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
        monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", url)
        assert run_smoke(url, "--check-edits") == 0
    assert "edited in LabArchives is not overwritten" in capsys.readouterr().out


def test_smoke_needs_an_existing_notebook(base_url: str, capsys) -> None:
    uri = "labarchives://" + base_url.removeprefix("http://")
    assert main(["--uri", uri, "--notebook", "Nope", "--auto-login"]) == 1
    assert "Nope" in capsys.readouterr().err


def test_smoke_reports_a_failed_login(base_url: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", "wrong")
    assert run_smoke(base_url) == 1
    assert "rejected the credentials" in capsys.readouterr().err


def test_manual_login_reads_the_pasted_redirect(base_url: str, monkeypatch) -> None:
    pasted = f"http://localhost:8089/callback?email={EMAIL}&auth_code={CODE}"
    monkeypatch.setattr("builtins.input", lambda prompt="": pasted)
    uri = "labarchives://" + base_url.removeprefix("http://")
    assert main(["--uri", uri, "--notebook", "Lab Notebook"]) == 0


def test_probe_network_failure_is_a_short_error_without_the_signature(
    base_url: str, monkeypatch, capsys
) -> None:
    import labapi
    import requests

    def fail(self, *args, **kwargs):
        raise requests.ConnectionError("GET http://x/api/tree_tools/get_tree_level?sig=SECRET")

    monkeypatch.setattr(labapi.User, "api_get", fail)
    assert run_smoke(base_url) == 1
    err = capsys.readouterr().err
    assert "probe failed (ConnectionError)" in err
    assert "SECRET" not in err


def test_marker_check_network_failure_leaks_no_signature() -> None:
    import requests

    from metarelay.backends.labarchives import LabArchivesSession
    from metarelay.core.errors import BackendUnavailableError
    from metarelay.core.path import LogicalPath
    from metarelay.testing.labarchives_smoke import _marker_kept

    def fail(*args, **kwargs):
        raise requests.ConnectionError("GET http://x/api/tree_tools/get_tree_level?sig=SECRET")

    session = LabArchivesSession.__new__(LabArchivesSession)
    session._walk = fail  # type: ignore[method-assign]
    with pytest.raises(BackendUnavailableError) as caught:
        _marker_kept(session, LogicalPath(("Lab Notebook", "smoke")), "smoke-1")
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None


# Recording cassettes


def test_record_writes_a_cassette_without_secrets(base_url: str, tmp_path: Path) -> None:
    path = tmp_path / "live.json"
    assert run_smoke(base_url, "--record", str(path)) == 0

    text = path.read_text()
    for secret in (AKPASS, AKID, CODE, EMAIL, "sig="):
        assert secret not in text
    interactions = load_cassette(path)
    calls = {interaction["api"] for interaction in interactions}
    assert {
        "users/user_access_info",
        "tree_tools/get_tree_level",
        "entries/add_attachment",
    } <= calls
    assert all("status" in interaction and "body" in interaction for interaction in interactions)


def test_shapes_list_the_elements_of_each_call(tmp_path: Path) -> None:
    shapes = xml_shapes(load_cassette(fake_cassette(tmp_path)))
    assert {"id", "notebooks/notebook/name", "notebooks/notebook/is-default"} <= shapes[
        "users/user_access_info"
    ]
    assert "node/tree-id" in shapes["tree_tools/insert_node"]
    assert "entries/entry/entry-data" in shapes["tree_tools/get_entries_for_page"]


def test_identical_cassettes_have_no_differences(tmp_path: Path) -> None:
    shapes = xml_shapes(load_cassette(fake_cassette(tmp_path)))
    assert compare_shapes(live=shapes, fake=shapes) == []


def test_differences_are_reported_both_ways() -> None:
    live = {"tree_tools/insert_node": {"node/tree-id", "node/created-at"}}
    fake = {"tree_tools/insert_node": {"node/tree-id", "node/is-page"}}
    differences = compare_shapes(live=live, fake=fake)
    assert any("node/created-at" in line and "fake does not" in line for line in differences)
    assert any("node/is-page" in line and "LabArchives does not" in line for line in differences)


def test_error_codes_are_read_from_error_responses(tmp_path: Path) -> None:
    codes = error_codes(load_cassette(fake_cassette(tmp_path)))
    assert codes == {"tree_tools/get_tree_level": {"4999"}}


def test_error_codes_are_read_inside_the_class_root() -> None:
    # What LabArchives sends: the error nested in <tree-tools>, beside the request.
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><tree-tools><error>'
        '<error-code type="integer">4999</error-code>'
        "<error-description>your request contained bad information</error-description>"
        "</error><request><class>tree_tools</class></request></tree-tools>"
    )
    live = [{"api": "tree_tools/get_tree_level", "status": 400, "body": body}]
    assert error_codes(live) == {"tree_tools/get_tree_level": {"4999"}}


def test_different_error_codes_are_reported() -> None:
    live = {"tree_tools/get_tree_level": {"4002"}, "a/b": {"1"}}
    fake = {"tree_tools/get_tree_level": {"4404"}, "a/b": {"1"}}
    assert compare_error_codes(live=live, fake=fake) == [
        "tree_tools/get_tree_level: LabArchives answered error 4002; the fake answered 4404."
    ]


def test_calls_only_in_one_cassette_are_reported() -> None:
    differences = compare_shapes(live={"a/b": {"x"}}, fake={})
    assert differences == ["a/b: LabArchives was called but the fake was not."]


@pytest.mark.skipif(not CASSETTES, reason="No cassettes captured from real LabArchives yet.")
@pytest.mark.parametrize("cassette", CASSETTES, ids=lambda path: path.stem)
def test_fake_matches_captured_cassettes(cassette: Path, tmp_path: Path) -> None:
    """Every element the fake sends must also come from LabArchives."""
    live = xml_shapes(load_cassette(cassette))
    fake = xml_shapes(load_cassette(fake_cassette(tmp_path)))
    problems = [
        line
        for line in compare_shapes(live=live, fake=fake)
        if line.endswith("LabArchives does not.")
    ]
    assert problems == []


@pytest.mark.skipif(not CASSETTES, reason="No cassettes captured from real LabArchives yet.")
@pytest.mark.parametrize("cassette", CASSETTES, ids=lambda path: path.stem)
def test_fake_error_codes_match_captured_cassettes(cassette: Path, tmp_path: Path) -> None:
    """Where both failed, the fake must answer with LabArchives' own error code."""
    live = error_codes(load_cassette(cassette))
    fake = error_codes(load_cassette(fake_cassette(tmp_path)))
    # The smoke script asks for a missing folder, so a cassette with no error
    # code means the code was not read, not that nothing failed.
    assert live.get("tree_tools/get_tree_level")
    assert compare_error_codes(live=live, fake=fake) == []


@pytest.mark.parametrize("cassette", CASSETTES, ids=lambda path: path.stem)
def test_committed_cassettes_hold_only_fake_data(cassette: Path) -> None:
    """Every element, attribute, and parameter is on a list, and holds its fake value."""
    interactions = load_cassette(cassette)
    assert leaks(interactions) == []

    again = json.loads(json.dumps(interactions))
    anonymize(again)
    assert again == interactions  # nothing was left to replace


# The dev stack's seed


def test_dev_stack_seed_loads_and_the_demo_user_logs_in() -> None:
    labapi = pytest.importorskip("labapi")
    seed = REPO / "docker" / "fake-labarchives" / "seed.json"
    fake = FakeLabArchives.from_seed(seed, akid="fake-akid", akpass="fake-akpass")
    with serve_fake(fake) as url:
        user = labapi.Client(url, "fake-akid", "fake-akpass").login("demo@example.org", "demo")
        assert "Lab Notebook" in user.notebooks.all_keys()


def test_seed_is_json_with_users() -> None:
    seed = json.loads((REPO / "docker" / "fake-labarchives" / "seed.json").read_text())
    assert seed["users"]


def one_call(body: str, **params: str) -> dict:
    return {
        "method": "GET",
        "api": "tree_tools/get_tree_level",
        "params": params,
        "status": 200,
        "content_type": "application/xml",
        "content_disposition": "",
        "body": body,
    }


def test_anonymize_replaces_what_identifies_the_person() -> None:
    call = one_call(
        "<users><fullname>Ada Lovelace</fullname><first-name>Ada</first-name>"
        "<last-name>Lovelace</last-name><orcid>0000-0001-2345-6789</orcid><sso-id></sso-id>"
        "<entry><last-modified-by>Ada Lovelace</last-modified-by>"
        "<last-modified-ip>203.0.113.7</last-modified-ip>"
        "<entry-url>https://real.example.org/share/Private%20notebook/abc</entry-url>"
        '<created-at type="datetime">2025-03-04T05:06:07Z</created-at>'
        '<version type="integer">2</version></entry></users>'
    )
    anonymize([call])
    for private in ("Ada", "Lovelace", "0000-0001", "203.0.113.7", "real.example", "Private"):
        assert private not in call["body"]
    assert "2025-03-04" not in call["body"]
    assert "<fullname>Demo User</fullname>" in call["body"]
    assert "<last-modified-ip>192.0.2.1</last-modified-ip>" in call["body"]
    assert '<created-at type="datetime">2026-01-01T00:00:00Z</created-at>' in call["body"]
    assert "<sso-id></sso-id>" in call["body"]  # an empty element stays empty
    assert '<version type="integer">2</version>' in call["body"]


def test_anonymize_keeps_only_the_names_the_script_created() -> None:
    made = one_call("<tree-tools><node/></tree-tools>", display_text="Smoke & tests")
    listed = one_call(
        "<users><notebook><name>Grant drafts</name></notebook>"
        "<level-node><display-text>Smoke &amp; tests</display-text></level-node>"
        "<level-node><display-text>Unpublished results</display-text></level-node>"
        "<level-node><display-text>Unpublished results</display-text></level-node></users>"
    )
    anonymize([listed, made])  # listed before the call that names it
    assert listed["body"] == (
        "<users><notebook><name>Notebook 1</name></notebook>"
        "<level-node><display-text>Smoke &amp; tests</display-text></level-node>"
        "<level-node><display-text>Item 1</display-text></level-node>"
        "<level-node><display-text>Item 1</display-text></level-node></users>"
    )


def test_anonymize_gives_an_id_the_same_fake_value_everywhere() -> None:
    notebook, page, entry = (
        "Tm90ZWJvb2svMTIzNDU2Nzg5",
        "VHJlZU5vZGUvOTg3NjU0MzIx",
        "RW50cnlQYXJ0LzE5MjEzMTM1",
    )
    calls = [
        one_call(f"<users><notebook><id>{notebook}</id></notebook></users>"),
        one_call(
            f"<entries><entry><eid>{entry}</eid><caption>preview {entry}</caption></entry>"
            f"<request><pid>{page}</pid></request></entries>",
            nbid=notebook,
            pid=page,
            parent_tree_id="0",
        ),
    ]
    calls[1]["content_disposition"] = f'attachment; filename="a eid={entry}.json"'
    anonymize(calls)

    assert calls[0]["body"] == "<users><notebook><id>notebook-1</id></notebook></users>"
    assert calls[1]["params"] == {"nbid": "notebook-1", "pid": "node-1", "parent_tree_id": "0"}
    assert calls[1]["body"] == (
        "<entries><entry><eid>entry-1</eid><caption>preview entry-1</caption></entry>"
        "<request><pid>node-1</pid></request></entries>"
    )
    assert calls[1]["content_disposition"] == 'attachment; filename="a eid=entry-1.json"'

    again = json.loads(json.dumps(calls))
    anonymize(again)
    assert again == calls  # doing it twice changes nothing more


def test_anonymize_replaces_a_short_id_where_it_is_the_whole_value() -> None:
    call = one_call(
        "<tree-tools><tree-id>MTIzNDU2Nzg5</tree-id><caption>size MTIzNDU2Nzg5</caption>"
        f"<parent-tree-id>0</parent-tree-id><pid>{MISSING_TREE_ID}</pid></tree-tools>",
        tree_id="MTIzNDU2Nzg5",
    )
    anonymize([call])
    assert call["params"] == {"tree_id": "node-1"}
    # Not inside other text: a short ID could be part of an unrelated word.
    assert call["body"] == (
        "<tree-tools><tree-id>node-1</tree-id><caption>size MTIzNDU2Nzg5</caption>"
        f"<parent-tree-id>0</parent-tree-id><pid>{MISSING_TREE_ID}</pid></tree-tools>"
    )
    assert leaks([call]) == []


def test_anonymize_skips_numbers_that_are_already_taken() -> None:
    call = one_call(
        "<t><display-text>Secret project</display-text><display-text>Item 1</display-text>"
        "<tree-id>VHJlZU5vZGUvOTg3NjU0MzIx</tree-id><tree-id>node-1</tree-id></t>"
    )
    anonymize([call])
    assert call["body"] == (
        "<t><display-text>Item 2</display-text><display-text>Item 1</display-text>"
        "<tree-id>node-2</tree-id><tree-id>node-1</tree-id></t>"
    )


@pytest.mark.parametrize(
    ("real", "fake"),
    [
        ("2026-11-16T14:03:22Z", "2026-01-01T00:00:00Z"),
        ("2026-11-16T14:03:22-05:00", "2026-01-01T00:00:00Z"),  # not the recorder's offset
        ("2026-11-16T14:03:22.123Z", "2026-01-01T00:00:00Z"),
        ("2026-11-16T14:03:22.123456+0530", "2026-01-01T00:00:00Z"),
        ("run 2026-11-16 14.46.21", "run 2026-01-01 00.00.00"),
        ("uploaded=2026-11-16T19_46_45Z.json", "uploaded=2026-01-01T00_00_00Z.json"),
    ],
)
def test_anonymize_replaces_the_whole_timestamp(real: str, fake: str) -> None:
    call = one_call(f"<e><caption>{real}</caption></e>")
    anonymize([call])
    assert call["body"] == f"<e><caption>{fake}</caption></e>"
    assert leaks([call]) == []


# What anonymize() does not know about must be reported, not passed through.


@pytest.mark.parametrize(
    ("body", "leak"),
    [
        (
            "<users><institution>NIMH Section on X</institution></users>",
            "the element <institution> is not on a list",
        ),
        (
            "<users><signature-name>Jane Q</signature-name></users>",
            "the element <signature-name> is not on a list",
        ),
        (
            '<users><notebook name="Grant drafts"/></users>',
            "the attribute 'name' on <notebook> is not on a list",
        ),
        ("<users><notebook/>Jane Q</users>", "there is text after <notebook>"),
        ("<users><caption>10.1.2.3</caption></users>", "an IP address (10.1.2.3)"),
        (
            "<users><caption>https://real.example.org/share/x</caption></users>",
            "a link (https://real.example.org)",
        ),
        ("<users><oops></users>", "the response is not XML, so it can't be checked"),
    ],
)
def test_leaks_reports_what_no_list_covers(body: str, leak: str) -> None:
    call = one_call(body)
    anonymize([call])
    assert leaks([call]) == [f"tree_tools/get_tree_level: {leak}"]


def test_leaks_reports_a_listed_value_that_is_still_real() -> None:
    call = one_call(
        "<users><fullname>Ada Lovelace</fullname><name>Grant drafts</name>"
        "<tree-id>VHJlZU5vZGUvOTg3NjU0MzIx</tree-id><uid>12345</uid>"
        "<created-at>2026-11-16T14:03:22-05:00</created-at></users>",
        nbid="Tm90ZWJvb2svMTIzNDU2Nzg5",
        uid="12345",
        client_ip="203.0.113.7",
    )
    assert leaks([call]) == [
        "tree_tools/get_tree_level: the parameter 'nbid' holds a real value",
        "tree_tools/get_tree_level: the parameter 'uid' holds a real value",
        "tree_tools/get_tree_level: the parameter 'client_ip' is not on a list",
        "tree_tools/get_tree_level: a real timestamp (2026-11-16T14:03:22-05:00)",
        "tree_tools/get_tree_level: <fullname> holds a real value",
        "tree_tools/get_tree_level: <name> holds a real value",
        "tree_tools/get_tree_level: <tree-id> holds a real value",
        "tree_tools/get_tree_level: <uid> holds a real value",
        "tree_tools/get_tree_level: <created-at> holds a real value",
    ]


def test_a_cassette_that_could_hold_real_data_is_not_written(tmp_path: Path, capsys) -> None:
    path = tmp_path / "old.json"
    call = one_call("<users><institution>NIMH Section on X</institution></users>")
    before = json.dumps({"version": 1, "interactions": [call]})
    path.write_text(before)

    assert cassettes_main([str(path)]) == 1

    assert path.read_text() == before
    err = capsys.readouterr().err
    assert "the element <institution> is not on a list" in err
    assert "NIMH" not in err  # the report names the element, not what it holds


def test_the_fake_sends_nothing_the_lists_do_not_cover(tmp_path: Path) -> None:
    assert leaks(load_cassette(fake_cassette(tmp_path))) == []


def test_recording_keeps_the_folders_the_smoke_script_made(base_url: str, tmp_path: Path) -> None:
    path = tmp_path / "live.json"
    assert run_smoke(base_url, "--record", str(path)) == 0
    text = path.read_text()
    assert "<display-text>MetaRelay smoke tests</display-text>" in text
    assert "Lab Notebook" not in text  # the script did not create the notebook
    assert "<name>Notebook 1</name>" in text


def test_a_cassette_recorded_earlier_can_be_anonymized_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.json"
    call = one_call(
        "<tree-tools><node><display-text>Mine</display-text></node>"
        "<level-node><display-text>Theirs</display-text></level-node>"
        "<last-modified-ip>203.0.113.7</last-modified-ip></tree-tools>",
        display_text="Mine",
    )
    path.write_text(json.dumps({"version": 1, "interactions": [call]}))

    assert cassettes_main([str(path)]) == 0

    [fake] = load_cassette(path)
    assert fake["body"] == (
        "<tree-tools><node><display-text>Mine</display-text></node>"
        "<level-node><display-text>Item 1</display-text></level-node>"
        "<last-modified-ip>192.0.2.1</last-modified-ip></tree-tools>"
    )


def test_redaction_keeps_longer_ids_intact(tmp_path: Path) -> None:
    body = "<users><id>U1</id><notebook><id>U12</id></notebook>ada@lab.org</users>"
    assert _redact_text(body, {"U1"}) == (
        "<users><id>REDACTED</id><notebook><id>U12</id></notebook>user@example.org</users>"
    )
