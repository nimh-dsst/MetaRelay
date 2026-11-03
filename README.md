# MetaRelay

MetaRelay is a backend-agnostic Python interface for writing NWB metadata to
electronic lab notebooks and other destinations. You pick a destination and
pass validated metadata records. MetaRelay handles the rest.

Planned backends:

- Local disk (always available)
- LabArchives, via [`labapi`](https://pypi.org/project/labapi/)
- eLabFTW, via [`elabapi-python`](https://pypi.org/project/elabapi-python/)
- GitHub, via the Contents API

Metadata schemas cover
[`ndx-fiber-photometry`](https://github.com/catalystneuro/ndx-fiber-photometry)
and [`ndx-microscopy`](https://github.com/catalystneuro/ndx-microscopy).

> **Status:** pre-alpha. The package scaffold is in place. The core
> interface and backends are in progress. See
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the design and roadmap.

## Install

MetaRelay is not on PyPI yet. Once it is, the base install carries only
`pydantic` and `pydantic-settings`. Each backend is an optional extra:

```sh
pip install metarelay                 # core, local backend, schemas
pip install "metarelay[labarchives]"  # adds the LabArchives backend
pip install "metarelay[all]"          # every backend, NWB builders, test suite
```

| Extra | Adds |
| --- | --- |
| `labarchives` | `labapi` |
| `elabftw` | `elabapi-python` |
| `github` | `httpx` |
| `nwb` | `pynwb` and the `ndx-*` extensions |
| `testing` | `pytest`, for the backend conformance suite |
| `all` | everything above |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, tests, and PR
expectations.

## License

Released under CC0 1.0 for now. The license is under review with DSST (plan
decision D1) and may change before the first release.
