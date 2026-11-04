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

```python
from pathlib import Path

import lab_schemas  # registers lab.implant
from metarelay import LogicalPath, Record, SyncIndex, connect

folder = Path("metadata")
folder.mkdir(exist_ok=True)
session = connect(folder.resolve().as_uri())  # file:///…/metadata

where = LogicalPath(("Fiber study", "Cohort A"))
session.ensure_container(where)

record = Record(
    schema_id="lab.implant",
    schema_version=1,
    record_id="implant-M1",
    context={"cohort": "A", "subject_id": "M1"},
    payload={"fiber_diameter_um": 400, "target_region": "VTA"},
)
receipt = session.write_record(where, record)

stored = session.read_record(where, "implant-M1")
print(stored.record.payload)  # {'fiber_diameter_um': 400.0, 'target_region': 'VTA'}
```

Keep the receipt. It lets the next write tell your own earlier write apart
from someone else's change:

```python
receipts = SyncIndex()
receipts.put(receipt)

changed = Record.model_validate(
    {**record.model_dump(), "payload": {"fiber_diameter_um": 200, "target_region": "VTA"}}
)
prior = receipts.get(session.destination, where, "implant-M1")
receipts.put(session.write_record(where, changed, prior_receipt=prior))
```

Without `prior_receipt`, the second write raises `ConflictError`, because
MetaRelay can't tell whether the record at the destination is yours.

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
