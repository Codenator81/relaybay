"""relaybay: a live board and MCP server for coordinating Claude Code sessions."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("relaybay")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
