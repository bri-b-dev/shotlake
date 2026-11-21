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
    """Standard: das Ziel (Handicap 18); `SHOTLAKE_BASELINE` überschreibt es."""
    return Path(os.environ.get("SHOTLAKE_BASELINE", "baselines/hcp18w-approx-v1.toml"))


@dataclass(frozen=True)
class Baseline:
    id: str
    status: str
    description: str
    tables: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]
    label: str = ""
    target_handicap: float | None = None

    @property
    def title(self) -> str:
        """Anzeigename für die Oberfläche (Standard: die ID)."""
        return self.label or self.id

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
    handicap = meta.get("target_handicap")
    return Baseline(
        str(meta["id"]),
        str(meta["status"]),
        str(meta["source"]).strip(),
        tables,
        label=str(meta.get("label", "")),
        target_handicap=float(handicap) if isinstance(handicap, int | float) else None,
    )


def list_baselines(directory: Path) -> list[Baseline]:
    """Alle Baselines eines Verzeichnisses, das schwächere Niveau (höheres Handicap) zuerst."""
    found = [load_baseline(p) for p in sorted(directory.glob("*.toml"))]
    return sorted(found, key=lambda b: (-(b.target_handicap or 0.0), b.id))


def resolve_baseline_path(spec: Path | str, directory: Path | None = None) -> Path:
    """Datei oder Kennung (`hcp18w-approx-v1`, Dateiname ohne `.toml`) zu einem Pfad auflösen."""
    path = Path(spec)
    if path.exists():
        return path
    folder = directory or default_baseline_path().parent
    for candidate in sorted(folder.glob("*.toml")):
        if candidate.stem == str(spec) or load_baseline(candidate).id == str(spec):
            return candidate
    known = sorted(p.stem for p in folder.glob("*.toml"))
    raise ValueError(f"Unbekannte Baseline {str(spec)!r}; verfügbar: {known}")
