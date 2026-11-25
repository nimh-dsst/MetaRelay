# MetaRelay

MetaRelay is a backend-agnostic Python interface for writing NWB metadata to
electronic lab notebooks and other destinations. You pick a destination and
pass validated metadata records. MetaRelay handles the rest.

Backends:

- Local disk (`file://`, always available)
- LabArchives, via [`labapi`](https://pypi.org/project/labapi/)
- eLabFTW, via [`elabapi-python`](https://pypi.org/project/elabapi-python/)
- GitHub, via the Git Data API (one commit per write)

> **Status:** pre-alpha. The API may change before 1.0. Built-in schemas for
> [`ndx-fiber-photometry`](https://github.com/catalystneuro/ndx-fiber-photometry)
> and [`ndx-microscopy`](https://github.com/catalystneuro/ndx-microscopy) are
> planned. Until then, you register your own.

## Install

MetaRelay is not on PyPI yet. Install it from the repository:

```sh
pip install "git+https://github.com/nimh-dsst/MetaRelay.git"
```

The base install carries only `pydantic` and `pydantic-settings`, and
includes the local backend. Each other backend is an optional extra:

```sh
pip install "metarelay[labarchives] @ git+https://github.com/nimh-dsst/MetaRelay.git"
```

| Extra | Adds |
| --- | --- |
| `labarchives` | `labapi` |
| `elabftw` | `elabapi-python` |
| `github` | `httpx` |
| `testing` | `pytest`, for the backend conformance suite |
| `all` | everything above |

## First write

Every record follows a registered schema. Save this as `lab_schemas.py`:

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

Then, next to it, write a record to a folder and read it back. Run this
from the folder that holds `lab_schemas.py`:

<!-- example: first_write.py#first-write -->
```python
from pathlib import Path

import lab_schemas  # noqa: F401  registers lab.implant

from metarelay import LogicalPath, Record, connect

folder = Path("metadata")
folder.mkdir(exist_ok=True)
session = connect(folder.resolve().as_uri())  # file:///…/metadata

where = LogicalPath(("Fiber study", "Cohort A"))
session.ensure_container(where)
session.write_record(
    where,
    Record(
        schema_id="lab.implant",
        schema_version=1,
        record_id="implant-M1",
        payload={"fiber_diameter_um": 400, "target_region": "VTA"},
    ),
)
print(session.read_record(where, "implant-M1").record.payload)
```

The same calls write to eLabFTW or GitHub: pass their URL and a config with
your token to `connect()`. LabArchives logs in through the browser first;
see its [page](https://nimh-dsst.github.io/MetaRelay/labarchives.html). To update a record safely, keep the receipt each write
returns: the [Quickstart](https://nimh-dsst.github.io/MetaRelay/quickstart.html)
shows how.

## Documentation

The docs are at <https://nimh-dsst.github.io/MetaRelay/>.

- [Quickstart](https://nimh-dsst.github.io/MetaRelay/quickstart.html): write
  a record to a folder, read it back, and update it.
- [Examples](https://nimh-dsst.github.io/MetaRelay/examples.html): runnable
  scripts for common tasks, including logging in to LabArchives. None needs an
  account.
- [Concepts](https://nimh-dsst.github.io/MetaRelay/concepts.html) and
  [architecture](https://nimh-dsst.github.io/MetaRelay/architecture.html): how
  MetaRelay works, and where each piece lives in the code.

## Contributing

See [`CONTRIBUTING.md`](https://github.com/nimh-dsst/MetaRelay/blob/main/CONTRIBUTING.md)
for setup, tests, and PR expectations.

## License

Released under the
[BSD 3-Clause License](https://github.com/nimh-dsst/MetaRelay/blob/main/LICENSE).
