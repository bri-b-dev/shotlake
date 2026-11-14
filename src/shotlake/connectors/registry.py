"""Register aller Connectors."""

from __future__ import annotations

from .base import Connector
from .garmin_range import GarminRangeConnector
from .mevo import MevoConnector

CONNECTORS: dict[str, Connector] = {c.id: c for c in (MevoConnector(), GarminRangeConnector())}


def get_connector(source: str) -> Connector:
    return CONNECTORS[source]
