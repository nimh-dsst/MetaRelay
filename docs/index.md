# MetaRelay

MetaRelay is a backend-agnostic Python library for writing NWB metadata
records to electronic lab notebooks, GitHub, and local disk. You pick a
destination and pass validated metadata records. MetaRelay handles the rest.

:::{note}
MetaRelay is pre-alpha: the API may change before 1.0. Built-in NWB schemas
are planned. Until then, you register your own.
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
```

```{toctree}
:caption: Design decisions
:maxdepth: 1
:glob:

adr/*
```
