"""The shared conflict rules. Cases mirror Chronoxiv's ``_upsert_record``."""

import pytest

from metarelay import ConflictError, Container, LogicalPath, Receipt, WriteAction, WriteMode
from metarelay.core.conflict import decide_write

LOCAL = "local"
PRIOR = "prior"
OTHER = "other"


def receipt(checksum: str = PRIOR, preview_checksum: str | None = None) -> Receipt:
    return Receipt(
        destination="file:///x",
        backend="file",
        container=Container(path=LogicalPath(("a",))),
        record_id="r",
        schema_id="test.fiber",
        schema_version=1,
        checksum=checksum,
        conflict_token=checksum,
        preview_checksum=preview_checksum,
    )


def decide(mode=WriteMode.UPSERT, remote=None, prior=None, remote_preview=None) -> WriteAction:
    return decide_write(
        mode=mode,
        local_checksum=LOCAL,
        remote_checksum=remote,
        prior_receipt=prior,
        remote_preview_checksum=remote_preview,
    )


@pytest.mark.parametrize("mode", list(WriteMode))
def test_nothing_at_the_destination_is_written_in_every_mode(mode: WriteMode) -> None:
    assert decide(mode=mode) is WriteAction.WRITE
    assert decide(mode=mode, prior=receipt()) is WriteAction.WRITE


def test_create_refuses_any_existing_record() -> None:
    with pytest.raises(ConflictError, match="already exists"):
        decide(mode=WriteMode.CREATE, remote=LOCAL)


def test_force_always_writes() -> None:
    assert decide(mode=WriteMode.FORCE, remote=OTHER) is WriteAction.WRITE
    assert (
        decide(mode=WriteMode.FORCE, remote=OTHER, prior=receipt(preview_checksum="p"))
        is WriteAction.WRITE
    )


def test_no_receipt_and_same_content_is_skipped() -> None:
    assert decide(remote=LOCAL) is WriteAction.SKIP


def test_no_receipt_and_different_content_is_a_conflict() -> None:
    with pytest.raises(ConflictError, match="different record already uses this ID"):
        decide(remote=OTHER)


def test_unchanged_remote_and_new_local_content_is_written() -> None:
    assert decide(remote=PRIOR, prior=receipt()) is WriteAction.WRITE


def test_nothing_changed_anywhere_is_skipped() -> None:
    assert decide(remote=LOCAL, prior=receipt(checksum=LOCAL)) is WriteAction.SKIP


def test_remote_already_matches_local_is_skipped() -> None:
    assert decide(remote=LOCAL, prior=receipt()) is WriteAction.SKIP


def test_remote_changed_since_receipt_is_a_conflict() -> None:
    with pytest.raises(ConflictError, match="changed at the destination"):
        decide(remote=OTHER, prior=receipt())


class TestPreviewEdits:
    """D7: an edit made directly in the notebook is reported, never overwritten."""

    def test_edited_preview_is_a_conflict(self) -> None:
        with pytest.raises(ConflictError, match="edited directly"):
            decide(remote=PRIOR, prior=receipt(preview_checksum="p1"), remote_preview="p2")

    def test_edited_preview_is_a_conflict_even_if_the_record_is_unchanged(self) -> None:
        with pytest.raises(ConflictError, match="edited directly"):
            decide(remote=LOCAL, prior=receipt(LOCAL, "p1"), remote_preview="p2")

    def test_untouched_preview_is_fine(self) -> None:
        assert (
            decide(remote=PRIOR, prior=receipt(preview_checksum="p1"), remote_preview="p1")
            is WriteAction.WRITE
        )

    def test_old_receipts_without_a_preview_checksum_are_not_checked(self) -> None:
        assert decide(remote=PRIOR, prior=receipt(), remote_preview="p2") is WriteAction.WRITE

    def test_backends_that_do_not_track_previews_are_not_checked(self) -> None:
        assert (
            decide(remote=PRIOR, prior=receipt(preview_checksum="p1"), remote_preview=None)
            is WriteAction.WRITE
        )


def test_write_mode_accepts_plain_strings() -> None:
    assert WriteMode("upsert") is WriteMode.UPSERT
