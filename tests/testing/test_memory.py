import pytest

from metarelay import ROOT, LogicalPath, NotFoundError, connect
from metarelay.core import backend as backend_module
from metarelay.testing import BackendConformance, MemoryBackend, MemorySession


class TestMemorySession(BackendConformance):
    @pytest.fixture
    def session(self) -> MemorySession:
        return MemorySession()

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
