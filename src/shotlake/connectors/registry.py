"""Register aller Connectors."""

from __future__ import annotations

from collections.abc import Callable

from .api import ApiConnector
from .arccos import ArccosConnector
from .base import Connector
from .bebrassie import BeBrassieConnector
from .garmin_platform import GarminPlatformConnector
from .garmin_range import GarminRangeConnector
from .mevo import MevoConnector

CONNECTORS: dict[str, Connector] = {c.id: c for c in (MevoConnector(), GarminRangeConnector())}


def get_connector(source: str) -> Connector:
    return CONNECTORS[source]


API_CONNECTORS: dict[str, Callable[[], ApiConnector]] = {
    GarminPlatformConnector.id: GarminPlatformConnector,
    ArccosConnector.id: ArccosConnector,
    BeBrassieConnector.id: BeBrassieConnector,
}
