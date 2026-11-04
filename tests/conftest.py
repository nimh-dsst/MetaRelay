from collections.abc import Iterator

import pydantic
import pytest

from metarelay.core import registry as registry_module
from metarelay.core.record import Record
from metarelay.core.registry import SchemaRegistry


class FiberPayload(pydantic.BaseModel):
    """A small stand-in schema. Real schemas arrive in #8-#10."""

    model_config = pydantic.ConfigDict(extra="forbid")

    diameter_um: float
    numerical_aperture: float | None = None


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[SchemaRegistry]:
    """Give every test a fresh default registry with one test schema in it."""
    fresh = SchemaRegistry()
    fresh.register("test.fiber", 1, FiberPayload)
    monkeypatch.setattr(registry_module, "default_registry", fresh)
    monkeypatch.setattr("metarelay.core.record.default_registry", fresh)
    yield fresh


@pytest.fixture
def make_record():
    def _make(record_id: str = "fiber-1", diameter_um: float = 400.0, **overrides) -> Record:
        fields = {
            "schema_id": "test.fiber",
            "schema_version": 1,
            "record_id": record_id,
            "context": {"cohort": "A", "subject_id": "M1"},
            "payload": {"diameter_um": diameter_um},
        }
        fields.update(overrides)
        return Record(**fields)

    return _make
