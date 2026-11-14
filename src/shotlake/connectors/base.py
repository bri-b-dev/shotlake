"""Gemeinsames Connector-Interface und Schema-Prüfung (Drift-Erkennung).

Jede Quelle ist ein Connector mit Metadaten (id, mode, risk), `discover`, `read_header`,
`parse` und `contract_check`. Datei-Connectors nutzen den Inhalts-Hash der Datei als
inkrementellen Cursor (bereits importierte Dateien werden übersprungen, siehe `ingest`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from ..model import ParsedSession


@dataclass(frozen=True)
class ContractReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def check_header(
    header: list[str],
    unit_row: list[str] | None,
    required: frozenset[str],
    known: frozenset[str],
    expected_units: dict[str, str],
) -> ContractReport:
    """Fehlende Pflichtspalten und geänderte Einheiten sind Fehler, neue Spalten nur Warnungen."""
    errors: list[str] = []
    warnings: list[str] = []
    if not header:
        return ContractReport(errors=("Leere Kopfzeile",))
    missing = sorted(required - set(header))
    if missing:
        errors.append(f"Pflichtspalten fehlen: {missing}")
    if expected_units:
        if unit_row is None:
            errors.append("Einheitenzeile fehlt")
        else:
            for col, expected in sorted(expected_units.items()):
                if col not in header:
                    continue
                i = header.index(col)
                actual = unit_row[i].strip() if i < len(unit_row) else ""
                if actual != expected:
                    errors.append(f"Einheit von {col}: erwartet {expected}, gefunden {actual!r}")
    unknown = sorted(set(header) - known)
    if unknown:
        warnings.append(f"Unbekannte Spalten: {unknown}")
    return ContractReport(errors=tuple(errors), warnings=tuple(warnings))


class Connector(ABC):
    id: ClassVar[str]
    mode: ClassVar[Literal["file", "api"]] = "file"
    risk: ClassVar[Literal["official", "unofficial_tos_risk", "file_only"]] = "file_only"
    description: ClassVar[str] = ""
    file_glob: ClassVar[str] = "*.csv"
    required_columns: ClassVar[frozenset[str]]
    known_columns: ClassVar[frozenset[str]]
    expected_units: ClassVar[dict[str, str]] = {}

    def discover(self, location: Path) -> list[Path]:
        """Dateien einer Quelle finden (Datei oder Verzeichnis), sortiert."""
        location = Path(location)
        if location.is_dir():
            return sorted(location.glob(self.file_glob))
        return [location]

    @abstractmethod
    def read_header(self, path: Path) -> tuple[list[str], list[str] | None]:
        """Kopfzeile und (falls vorhanden) Einheitenzeile lesen, ohne Daten zu parsen."""

    @abstractmethod
    def parse(self, path: Path) -> ParsedSession:
        """Datei parsen; bei Schema-Abweichung `SchemaDriftError`."""

    def contract_check(self, path: Path) -> ContractReport:
        header, units = self.read_header(path)
        return check_header(
            header, units, self.required_columns, self.known_columns, self.expected_units
        )
