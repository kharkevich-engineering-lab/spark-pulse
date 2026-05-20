"""Get package version dynamically."""

import importlib.metadata

__version__ = importlib.metadata.version("spark-pulse")


def get_version() -> str:
    """Return the installed package version."""
    return __version__
