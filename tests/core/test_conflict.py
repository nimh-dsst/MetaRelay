"""The shared conflict rules. Cases mirror Chronoxiv's ``_upsert_record``."""

import pytest

from metarelay import (
    ConflictError,
    Container,
    LogicalPath,
    Receipt,
    ValidationError,
    WriteAction,
    WriteMode,
)
from metarelay.core.conflict import decide_write
from metarelay.core.preview import PreviewDiff, PreviewEdit

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


def decide(
    mode=WriteMode.UPSERT, remote=None, prior=None, remote_preview=None, **kwargs
) -> WriteAction:
    return decide_write(
        mode=mode,
        local_checksum=LOCAL,
        remote_checksum=remote,
        prior_receipt=prior,
        remote_preview_checksum=remote_preview,
        **kwargs,
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


def test_remote_already_matches_local_is_skipped() -> None:
    # Whether the receipt is from before this change or already matches it.
    assert decide(remote=LOCAL, prior=receipt()) is WriteAction.SKIP
    assert decide(remote=LOCAL, prior=receipt(checksum=LOCAL)) is WriteAction.SKIP


def test_remote_changed_since_receipt_is_a_conflict() -> None:
    with pytest.raises(ConflictError, match="changed at the destination"):
        decide(remote=OTHER, prior=receipt())


class TestPreviewEdits:
    """D7: an edit made directly in the notebook is reported, never overwritten."""

    def test_edited_preview_is_a_conflict(self) -> None:
        with pytest.raises(ConflictError, match="preview was edited directly"):
            decide(remote=PRIOR, prior=receipt(preview_checksum="p1"), remote_preview="p2")

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


@pytest.mark.parametrize("mode", ["create", "upsert", "force"])
def test_mode_as_a_string_acts_like_the_enum(mode: str) -> None:
    # "create" used to fail the ``is`` checks and act like upsert.
    for remote, prior in [(LOCAL, None), (OTHER, receipt(OTHER)), (OTHER, None)]:
        try:
            expected = decide(WriteMode(mode), remote=remote, prior=prior)
        except ConflictError:
            with pytest.raises(ConflictError):
                decide(mode, remote=remote, prior=prior)
        else:
            assert decide(mode, remote=remote, prior=prior) is expected


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown write mode 'crate'"):
        decide("crate", remote=LOCAL)


def test_preview_conflict_advice_works() -> None:
    # An edited preview is a conflict even if the record itself is unchanged.
    # The old message said "copy the change into the record", but only force clears it.
    prior = receipt(LOCAL, "p1")
    with pytest.raises(ConflictError, match="mode='force'") as caught:
        decide(remote=LOCAL, prior=prior, remote_preview="p2")
    assert "edited directly" in str(caught.value)
    assert "copy it into the record first" in str(caught.value)
    assert decide(WriteMode.FORCE, remote=LOCAL, prior=prior, remote_preview="p2") is (
        WriteAction.WRITE
    )


def test_a_preview_edit_conflict_carries_the_diff() -> None:
    diff = PreviewDiff(edits=[], complete=False)
    with pytest.raises(ConflictError, match="can't tell what changed") as raised:
        decide_write(
            mode=WriteMode.UPSERT,
            local_checksum=LOCAL,
            remote_checksum=PRIOR,
            prior_receipt=receipt(preview_checksum="p1"),
            remote_preview_checksum="p2",
            preview_diff=lambda: diff,
        )
    assert raised.value.preview_diff is diff
    # A conflict over the record itself carries no diff.
    with pytest.raises(ConflictError) as raised:
        decide(remote=OTHER, prior=receipt())
    assert raised.value.preview_diff is None


def test_a_failing_diff_still_raises_the_conflict() -> None:
    def broken() -> PreviewDiff:
        raise KeyError("renderer broke")

    with pytest.raises(ConflictError, match="edited directly") as raised:
        decide_write(
            mode=WriteMode.UPSERT,
            local_checksum=LOCAL,
            remote_checksum=PRIOR,
            prior_receipt=receipt(preview_checksum="p1"),
            remote_preview_checksum="p2",
            preview_diff=broken,
        )
    assert raised.value.preview_diff is None


# Accepting notebook edits


def test_accept_edits_writes_the_record_with_its_edits(make_record) -> None:
    from metarelay import LogicalPath as Path_
    from metarelay import accept_edits
    from metarelay.testing.memory import MemorySession

    session, where = MemorySession(), Path_(("Study",))
    session.ensure_container(where)
    receipt = session.write_record(where, make_record(diameter_um=400))
    session.edit_preview(
        where, "fiber-1", session.preview_of(where, "fiber-1").replace("400.0", "250")
    )
    with pytest.raises(ConflictError) as raised:
        session.write_record(where, make_record(diameter_um=400), prior_receipt=receipt)

    written = accept_edits(session, where, make_record(diameter_um=400), raised.value.preview_diff)
    assert written["fiber-1"].record.payload == {"diameter_um": 250.0}
    # The receipt describes the destination now, so the next write goes through.
    session.write_record(
        where, make_record(diameter_um=250), prior_receipt=written["fiber-1"].receipt
    )


def test_accept_edits_writes_nothing_when_it_cant_accept_everything(make_record) -> None:
    from metarelay import LogicalPath as Path_
    from metarelay import accept_edits
    from metarelay.core.preview import PreviewEdit
    from metarelay.testing.memory import MemorySession

    session, where = MemorySession(), Path_(("Study",))
    session.ensure_container(where)
    session.write_record(where, make_record(diameter_um=400))
    edit = PreviewEdit(
        record_id="fiber-1", table="Metadata", field="diameter_um", written="400.0", current="wide"
    )
    with pytest.raises(ValidationError, match="isn't a number"):
        accept_edits(session, where, make_record(), PreviewDiff(edits=[edit], complete=True))
    with pytest.raises(ConflictError, match="can't list"):
        accept_edits(session, where, make_record(), PreviewDiff(edits=[edit], complete=False))
    assert session.read_record(where, "fiber-1").record.payload == {"diameter_um": 400.0}


class TestEntryEdits:
    """Several records share one preview (an eLabFTW entry): a hand edit to it
    blocks every write into the entry until forced."""

    @staticmethod
    def decide(mode=WriteMode.UPSERT, remote=None, prior=None, **kwargs) -> WriteAction:
        return decide(mode, remote, prior, entry_edited=True, **kwargs)

    @pytest.mark.parametrize(
        ("mode", "prior"),
        [(WriteMode.UPSERT, None), (WriteMode.CREATE, None), (WriteMode.UPSERT, receipt())],
        ids=["upsert", "create", "deleted since the receipt"],
    )
    def test_it_blocks_a_new_record(self, mode, prior) -> None:
        with pytest.raises(ConflictError, match="entry holding this record was edited directly"):
            self.decide(mode=mode, prior=prior)

    def test_it_blocks_a_changed_record(self) -> None:
        with pytest.raises(ConflictError, match="edited directly"):
            self.decide(remote=PRIOR, prior=receipt())

    def test_it_blocks_an_unchanged_record_with_a_receipt(self) -> None:
        # The caller thinks the entry is as MetaRelay left it; it isn't.
        with pytest.raises(ConflictError, match="edited directly"):
            self.decide(remote=LOCAL, prior=receipt(LOCAL))

    def test_identical_content_with_no_receipt_is_not_a_conflict(self) -> None:
        assert self.decide(remote=LOCAL) is WriteAction.SKIP

    @pytest.mark.parametrize("remote", [None, PRIOR])
    def test_force_writes(self, remote) -> None:
        assert self.decide(mode=WriteMode.FORCE, remote=remote) is WriteAction.WRITE

    def test_record_conflicts_come_first(self) -> None:
        with pytest.raises(ConflictError, match="already exists"):
            self.decide(mode=WriteMode.CREATE, remote=PRIOR)
        with pytest.raises(ConflictError, match="changed at the destination"):
            self.decide(remote="someone else's", prior=receipt())

    def test_the_diff_names_other_records(self) -> None:
        other = PreviewEdit(
            record_id="rec-2", table="Metadata", field="site", written="NAc", current="VTA"
        )
        diff = PreviewDiff(edits=[other], complete=True)
        with pytest.raises(ConflictError, match="site of rec-2: 'NAc' → 'VTA'") as raised:
            self.decide(preview_diff=lambda: diff, record_id="rec-1")
        assert raised.value.preview_diff == diff

    def test_a_failing_diff_still_raises_the_conflict(self) -> None:
        def broken() -> PreviewDiff:
            raise KeyError("odd entry")

        with pytest.raises(ConflictError, match="edited directly") as raised:
            self.decide(preview_diff=broken)
        assert raised.value.preview_diff is None

    @pytest.mark.parametrize(
        ("edited", "mode", "remote", "prior", "remote_preview"),
        [
            (False, WriteMode.UPSERT, None, None, None),
            (False, WriteMode.UPSERT, PRIOR, receipt(), None),
            (False, WriteMode.UPSERT, PRIOR, receipt(preview_checksum="p1"), "p1"),
            (True, WriteMode.UPSERT, LOCAL, None, None),
            (True, WriteMode.FORCE, PRIOR, receipt(), None),
        ],
        ids=[
            "not edited, new",
            "not edited, update",
            "untouched preview",
            "identical, no receipt",
            "force",
        ],
    )
    def test_the_diff_is_built_only_for_a_conflict(
        self, edited, mode, remote, prior, remote_preview
    ):
        # Building it reads and renders the whole entry: not on every write.
        calls: list[None] = []
        decide(
            mode,
            remote,
            prior,
            remote_preview,
            entry_edited=edited,
            preview_diff=lambda: calls.append(None),
        )
        assert calls == []
