# MetaRelay

MetaRelay is a backend-agnostic Python library for writing NWB metadata
records to electronic lab notebooks, GitHub, and local disk. You pick a
destination and pass validated metadata records. MetaRelay handles the rest.

:::{note}
MetaRelay is pre-alpha. The core interface and the local, LabArchives,
eLabFTW, and GitHub backends are in progress.
:::

New here? Start with the [Quickstart](quickstart.md), then read
[Concepts](concepts.md) for the vocabulary.

```{toctree}
:caption: Use MetaRelay
:maxdepth: 1

quickstart
concepts
examples
```

```{toctree}
:caption: Backends
:maxdepth: 1

labarchives
elabftw
github
```

```{toctree}
:caption: Develop MetaRelay
:maxdepth: 1

contributing
architecture
IMPLEMENTATION_PLAN
adr/0001-elabftw-record-storage
```
