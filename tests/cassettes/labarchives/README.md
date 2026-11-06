# LabArchives cassettes

Recorded traffic from real LabArchives, captured by DSST with:

    python -m metarelay.testing.labarchives_smoke --notebook "<test notebook>" --record <name>.json

`tests/testing/test_labarchives_smoke.py::test_fake_matches_captured_cassettes`
checks each cassette here against the fake server: every XML element the fake
sends must also come from LabArchives. When it fails, fix the fake.

Recording redacts the API key, signatures, login email and auth code, user ID,
and email addresses, and stores attachment downloads by size only. Notebook,
folder, and entry names remain: review a cassette before committing it.
