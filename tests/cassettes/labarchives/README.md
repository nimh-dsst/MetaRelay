# LabArchives cassettes

Recorded traffic from real LabArchives, captured by DSST with:

    python -m metarelay.testing.labarchives_smoke --notebook "<test notebook>" --record <name>.json

`tests/testing/test_labarchives_smoke.py::test_fake_matches_captured_cassettes`
checks each cassette here against the fake server: every XML element the fake
sends must also come from LabArchives.
`test_fake_error_codes_match_captured_cassettes` checks that the fake answers
the missing-folder request with the same error code. When either fails, fix
the fake.

A cassette here holds no real data. This repository is public. Recording:

- redacts the API key, signatures, login email and auth code, and user ID
- replaces each notebook, folder, page, and entry ID with a fake one
  (`notebook-1`, `node-1`, `entry-1`). Two IDs that name nothing real stay:
  `0`, the top level, and `999999999`, the smoke script's missing folder
- replaces the person's name, ORCID, and IP address, share links, and every
  timestamp, with its fraction and timezone, with fake values
- renames every notebook, folder, and page the script did not create
  (`Notebook 1`, `Item 1`)
- stores attachment downloads by size only

What stays is the shape of each response, LabArchives' fixed values (error
codes, part types, flags), and what the smoke script wrote. An ID inside a
caption or file name the script wrote is replaced only when it is 16
characters or longer, as LabArchives' IDs are.

Recording fails closed. Every element, attribute, and request parameter must
be on a list in `metarelay/testing/labarchives_cassettes.py` that says what it
holds. If LabArchives sends one that is not, no cassette is written and the
error names it. `test_committed_cassettes_hold_only_fake_data` runs the same
check on every cassette here. Review a cassette before committing it anyway.

To replace the values in a cassette recorded before these rules:

    python -m metarelay.testing.labarchives_cassettes <name>.json

| Cassette | Holds |
| --- | --- |
| `labarchives-live.json` | A smoke run against real LabArchives, recorded by DSST on November 16, 2026. The responses have the shape LabArchives sent. Every value from the account is fake |
