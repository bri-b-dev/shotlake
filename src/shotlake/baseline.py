"""Baseline für Strokes Gained: erwartete Restschläge je (Lage, Distanz), TOML-Datei."""

from __future__ import annotations

import os
import tomllib
from bisect import bisect_right
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

LIES = ("tee", "fairway", "rough", "sand", "green")


def default_baseline_path() -> Path:
    return Path(os.environ.get("SHOTLAKE_BASELINE", "baselines/hcp18w-approx-v1.toml"))


@dataclass(frozen=True)
class Baseline:
    id: str
    status: str
    description: str
    tables: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]

    def expected(self, lie: str, distance_m: float) -> float | None:
        """Erwartete Restschläge; None, wenn die Lage keine Tabelle hat (z. B. `other`).

        Linear interpoliert, unterhalb der ersten Stützstelle konstant, oberhalb mit der
        Steigung des letzten Abschnitts fortgesetzt.
        """
        table = self.tables.get(lie)
        if table is None:
            return None
        xs, ys = table
        if distance_m <= xs[0]:
            return ys[0]
        i = bisect_right(xs, distance_m)
        if i >= len(xs):
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return ys[-1] + slope * (distance_m - xs[-1])
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        return y0 + (y1 - y0) * (distance_m - x0) / (x1 - x0)


def load_baseline(path: Path) -> Baseline:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    meta = raw.get("meta", {})
    for key in ("id", "source", "license", "status"):
        if not meta.get(key):
            raise ValueError(f"{path}: meta.{key} fehlt (Quelle und Lizenz sind Pflicht)")
    tables: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    for lie, t in raw.get("tables", {}).items():
        if lie not in LIES:
            raise ValueError(f"{path}: unbekannte Lage {lie!r}")
        xs, ys = tuple(map(float, t["distance_m"])), tuple(map(float, t["expected"]))
        if len(xs) != len(ys) or len(xs) < 2:
            raise ValueError(f"{path}: Tabelle {lie} braucht mindestens 2 gleich lange Spalten")
        if any(b <= a for a, b in pairwise(xs)):
            raise ValueError(f"{path}: Distanzen von {lie} müssen streng aufsteigen")
        if any(b < a for a, b in pairwise(ys)):
            raise ValueError(f"{path}: erwartete Schläge von {lie} dürfen nicht fallen")
        tables[lie] = (xs, ys)
    missing = [lie for lie in LIES if lie not in tables]
    if missing:
        raise ValueError(f"{path}: Tabellen fehlen für {missing}")
    return Baseline(str(meta["id"]), str(meta["status"]), str(meta["source"]).strip(), tables)
