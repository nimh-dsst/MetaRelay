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
- replaces every notebook, folder, page, and entry ID with a fake one
  (`notebook-1`, `node-1`, `entry-1`)
- replaces the person's name, ORCID, and IP address, share links, and every
  timestamp with fake values
- renames every notebook, folder, and page the script did not create
  (`Notebook 1`, `Item 1`)
- stores attachment downloads by size only

What stays is the shape of each response, LabArchives' fixed values (error
codes, part types, flags), and what the smoke script wrote. Review a cassette
before committing it anyway. `test_committed_cassettes_hold_only_fake_data`
fails if one still holds a value the recorder would replace.

To replace the values in a cassette recorded before these rules:

    python -m metarelay.testing.labarchives_cassettes <name>.json

| Cassette | Holds |
| --- | --- |
| `labarchives-fake.json` | The shapes of a smoke run against real LabArchives, recorded by DSST on November 16, 2026. Every value from the account is fake |
