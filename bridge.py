"""Backwards-compatible entry point.

The systemd unit and everyone's muscle memory run `uvicorn bridge:app`,
so the whole service is still reachable from this module -- it is now a
thin re-export over the `flowbridge` package.
"""
from flowbridge.api import app, create_app  # noqa: F401

__all__ = ["app", "create_app"]
