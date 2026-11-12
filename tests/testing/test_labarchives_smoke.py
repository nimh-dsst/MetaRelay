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
    compare_error_codes,
    compare_shapes,
    error_codes,
    load_cassette,
    xml_shapes,
)
from metarelay.testing.labarchives_smoke import main

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
    assert "missing folder with error code 4404" in out


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
    assert codes == {"tree_tools/get_tree_level": {"4404"}}


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
    """The fake's error codes are placeholders until a real cassette shows LabArchives' own."""
    live = error_codes(load_cassette(cassette))
    fake = error_codes(load_cassette(fake_cassette(tmp_path)))
    assert compare_error_codes(live=live, fake=fake) == []


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


def test_redaction_keeps_longer_ids_intact(tmp_path: Path) -> None:
    from metarelay.testing.labarchives_cassettes import _redact_text

    body = "<users><id>U1</id><notebook><id>U12</id></notebook>ada@lab.org</users>"
    assert _redact_text(body, {"U1"}) == (
        "<users><id>REDACTED</id><notebook><id>U12</id></notebook>user@example.org</users>"
    )
