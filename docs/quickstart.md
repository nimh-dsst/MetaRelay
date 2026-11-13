# Quickstart

This page writes a metadata record to a folder on your computer, reads it
back, and updates it. No account or network is needed: the local backend
(`file://`) ships with every install.

## Install

MetaRelay is not on PyPI yet. Install it from the repository:

```sh
pip install "git+https://github.com/nimh-dsst/MetaRelay.git"
```

## Register a schema

Every record follows a registered schema. MetaRelay's own schemas are not
written yet, so register a small one. Save this as `lab_schemas.py`:

<!-- example: lab_schemas.py#schema -->
```python
import pydantic

from metarelay import register_schema


class Implant(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")  # required: see Concepts

    fiber_diameter_um: float
    target_region: str


register_schema("lab.implant", 1, Implant)
```

## From Python

The code below comes from the
[`save_metadata` example](../examples/save_metadata/README.md). To run all of
it at once, from a clone of the repository:

```sh
mkdir -p metadata
python -m examples.save_metadata "file://$PWD/metadata"
```

Connect to a folder, and pick where the record goes:

```python
from pathlib import Path

import lab_schemas  # registers lab.implant
from metarelay import (
    ConflictError,
    LogicalPath,
    Receipt,
    Record,
    Session,
    SyncIndex,
    WriteMode,
    connect,
)

folder = Path("metadata")
folder.mkdir(exist_ok=True)
session = connect(folder.resolve().as_uri())  # file:///…/metadata

where = LogicalPath(("Fiber study", "Cohort A"))
receipts = SyncIndex()  # save it with your data: see below
force = False  # True only after the person chose to replace a change made elsewhere
```

Every write returns a receipt. Keep it, and pass it to the next write of the
same record: it lets MetaRelay tell your own earlier write apart from
someone else's change. The example wraps that in one function:

<!-- example: save_metadata/save_metadata.py#write-with-receipt -->
```python
def write(
    session: Session,
    where: LogicalPath,
    record: Record,
    receipts: SyncIndex,
    *,
    force: bool = False,
) -> Receipt:
    """Write ``record`` with the receipt from the last write, and keep the new one.

    Raises:
        ConflictError: If the record changed at the destination since then,
            unless ``force`` is set.
    """
    prior = receipts.get(session.destination, where, record.record_id)
    try:
        receipt = session.write_record(where, record, prior_receipt=prior)
    except ConflictError:
        # Someone changed the record at the destination since `prior`.
        # Stop, unless the person already chose to replace their change.
        if not force:
            raise
        receipt = session.write_record(where, record, mode=WriteMode.FORCE)
    receipts.put(receipt)
    return receipt
```

Write a record and read it back:

<!-- example: save_metadata/save_metadata.py#write-and-read -->
```python
session.ensure_container(where)

record = Record(
    schema_id="lab.implant",
    schema_version=1,
    record_id="implant-M1",
    context={"cohort": "A", "subject_id": "M1"},
    payload={"fiber_diameter_um": 400, "target_region": "VTA"},
)
written = write(session, where, record, receipts, force=force)

stored = session.read_record(where, "implant-M1")
# stored.record.payload == {"fiber_diameter_um": 400.0, "target_region": "VTA"}
```

Records are frozen. To change one, validate a new one and write it. The
receipt from the first write makes this an update, not a conflict:

<!-- example: save_metadata/save_metadata.py#update -->
```python
changed = Record.model_validate(
    {**record.model_dump(), "payload": {"fiber_diameter_um": 200, "target_region": "VTA"}}
)
updated = write(session, where, changed, receipts)
```

Without the receipt, the second write raises `ConflictError`, because
MetaRelay can't tell whether the record at the destination is yours. So save
the receipts between runs. A `SyncIndex` is plain data:

```python
Path("receipts.json").write_text(receipts.model_dump_json())
receipts = SyncIndex.model_validate_json(Path("receipts.json").read_text())
```

[Concepts](concepts.md#write-modes) covers what to do on a conflict.

## What's on disk

```
metadata/
└── Fiber study/
    └── Cohort A/
        ├── implant-M1.json      the record
        ├── implant-M1.md        a readable table of it
        └── .metarelay/          index and write history
```

## From the command line

The same steps with the `metarelay` command. `--import` loads the module that
registers your schema.

```sh
cat > implant-M1.json <<'EOF'
{"schema_id": "lab.implant", "schema_version": 1, "record_id": "implant-M1",
 "context": {"cohort": "A", "subject_id": "M1"},
 "payload": {"fiber_diameter_um": 400, "target_region": "VTA"}}
EOF

mkdir -p metadata
DEST="file://$PWD/metadata"
metarelay mkdir "$DEST" "Fiber study/Cohort A"
metarelay --import lab_schemas write "$DEST" "Fiber study/Cohort A" implant-M1.json \
    --receipts receipts.json
metarelay --import lab_schemas ls "$DEST" "Fiber study/Cohort A"
metarelay --import lab_schemas read "$DEST" "Fiber study/Cohort A" implant-M1
```

| Command | Does |
| --- | --- |
| `ls URI [PATH]` | List containers (ending in `/`) and records |
| `mkdir URI PATH` | Create a container and its parents |
| `write URI PATH FILE` | Write a record from JSON (`-` reads standard input) |
| `read URI PATH ID` | Print a record as JSON |

`write` takes `--mode create|upsert|force`. It exits with status 3 on a
conflict and 1 on any other error.

`write` won't create a missing container, so a typo in the path is an error
instead of a new folder. Run `mkdir` first, or pass `--create-path`.

`mkdir` and `write --create-path` take `--holds-records` for a container that
will hold records and never other containers. A backend that has a separate
kind of container for that (a notebook page) creates one; local folders are
unaffected. If the container already exists, the flag changes nothing.
