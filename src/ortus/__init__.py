"""ortus: global Python CLI for bd-driven Claude Code workflows."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("ortus")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
