import pytest

from metarelay import ROOT, LogicalPath, NotFoundError, connect
from metarelay.core import backend as backend_module
from metarelay.testing import BackendConformance, MemoryBackend, MemorySession


class TestMemorySession(BackendConformance):
    records_only_is_a_leaf = True

    @pytest.fixture
    def session(self) -> MemorySession:
        return MemorySession()

    def edit_preview(self, session, path, record_id, text) -> None:
        session.edit_preview(path, record_id, text)


class _OnlyUnderBase(MemorySession):
    """Refuses to create anything outside ``Lab Notebook``, like LabArchives."""

    def __init__(self) -> None:
        super().__init__()
        super().ensure_container(LogicalPath(("Lab Notebook",)))

    def ensure_container(self, path, *, holds_records=False):
        if path.parts[:1] != ("Lab Notebook",):
            raise AssertionError(f"the suite created {path} outside its base")
        return super().ensure_container(path, holds_records=holds_records)


class TestConformanceStaysUnderItsBase(BackendConformance):
    base = LogicalPath(("Lab Notebook",))
    records_only_is_a_leaf = True

    @pytest.fixture
    def session(self) -> MemorySession:
        return _OnlyUnderBase()

    def edit_preview(self, session, path, record_id, text) -> None:
        session.edit_preview(path, record_id, text)


def test_preview_is_stored_with_the_record(make_record) -> None:
    session = MemorySession()
    receipt = session.write_record(ROOT, make_record())
    assert "| diameter_um | 400.0 |" in session.preview_of(ROOT, "fiber-1")
    assert receipt.preview_checksum is not None


def test_writing_into_a_missing_container_is_not_found(make_record) -> None:
    with pytest.raises(NotFoundError):
        MemorySession().write_record(LogicalPath(("Nope",)), make_record())


def test_backend_reuses_the_session_for_a_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    monkeypatch.setattr(backend_module, "load_backend", lambda scheme: backend)
    assert connect("memory://a") is connect("Memory://a/")
    assert connect("memory://a") is not connect("memory://b")
