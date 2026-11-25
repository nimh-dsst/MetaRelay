"""Write one record to a folder and read it back: the README's first write.

Run it as a script, so that ``lab_schemas`` imports from this folder. From
any folder::

    python path/to/examples/first_write.py

It writes to ``./metadata``.
"""

# [docs:first-write]
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
# [/docs:first-write]
