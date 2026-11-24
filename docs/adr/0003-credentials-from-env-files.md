# ADR 0003: Reading credentials from a `.env` file

- **Status:** Accepted 2026-11-24.
- **Issue:** #39.

## Context

DSST expects people to keep an API key in a `.env` file, for eLabFTW and
GitHub. Configs read environment variables named for their service, such as
`METARELAY_ELABFTW_TOKEN`, but read a `.env` file only when the caller passes
`_env_file=`. The docs didn't say so.

ChronoXIV calls `labapi.Client()` with no arguments, and `labapi` loads `.env`
itself and reads `ACCESS_KEYID`, `ACCESS_PWD`, and `API_URL`. MetaRelay passes
credentials to `labapi` explicitly and reads `METARELAY_LABARCHIVES_*`, so
those `.env` files stop working after the switch.

## Decision

1. **MetaRelay doesn't read `.env` on its own.** `core/config.py` keeps its
   rule that MetaRelay never looks for credentials. A hosted ChronoXIV
   supplies them through the server environment.
2. **`_env_file=` is a supported, documented argument**, despite the
   underscore that `pydantic-settings` gives it: `ElabFTWConfig(_env_file=".env")`.
   A config reads only the variables with its own prefix from the file, so
   one `.env` can hold other apps' settings. Apps that already use
   `python-dotenv` can keep loading `.env` themselves.
3. **`labapi`'s names are not accepted.** `ACCESS_KEYID`, `ACCESS_PWD`, and
   `API_URL` are too generic, the same reason there is no shared
   `METARELAY_TOKEN`. Instead, when the access key ID is missing and
   `ACCESS_KEYID` is set, the `ConfigurationError` names
   `METARELAY_LABARCHIVES_AKID` and `METARELAY_LABARCHIVES_AKPASS`, never the
   value. `docs/labarchives.md` has a migration note, and ChronoXIV maps the
   old names in its own shim during the switch (nimh-dsst/chronoxiv#68).
4. **No `--env-file` option on the CLI for now.** The CLI calls
   `connect(args.uri)` with no config, so adding one later is small. Revisit
   if DSST asks.

## Consequences

- Credentials reach MetaRelay only from explicit values, the environment, or
  a file the caller names. Nothing is picked up from the working folder.
- Variables already set in the environment win over the file.
- The `.env` hint looks only at the environment: an old-style `.env` passed
  with `_env_file=` gets the plain missing-field error.
- `pydantic-settings` 2.14 is the minimum, for `dotenv_filtering`.
