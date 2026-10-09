"""Data-source adapters. Each adapter turns an external feed into SourceSnapshots."""

from .base import SnapshotRef, SourceCreature, SourceFormatError, SourceSnapshot, SourceTeam
from .jwa_dashboard_feed import JwaDashboardFeed

__all__ = [
    "JwaDashboardFeed",
    "SnapshotRef",
    "SourceCreature",
    "SourceFormatError",
    "SourceSnapshot",
    "SourceTeam",
    "default_sources",
]


def default_sources():
    """The automatic sources used by scheduled collection."""
    return [JwaDashboardFeed()]
