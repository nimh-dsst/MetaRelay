"""MetaRelay: write NWB metadata records to lab notebooks, GitHub, and local disk."""

from importlib.metadata import version

from metarelay.core import *  # noqa: F403
from metarelay.core import __all__ as _core_all

__version__ = version("metarelay")

__all__ = ["__version__", *_core_all]
