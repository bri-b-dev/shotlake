"""Abbildung quellspezifischer Schlägerbezeichnungen auf kanonische Schlägertypen.

Unbekannte Bezeichnungen ergeben None (nie raten); der Aufrufer markiert sie mit `club_unmapped`.
"""

from __future__ import annotations

import re

_FIXED = {
    "driver": "driver",
    "pitching wedge": "wedge_pitching",
    "pitching-wedge": "wedge_pitching",
    "gap wedge": "wedge_gap",
    "gap-wedge": "wedge_gap",
    "sand wedge": "wedge_sand",
    "sandwedge": "wedge_sand",
    "sand-wedge": "wedge_sand",
    "lob wedge": "wedge_lob",
    "lobwedge": "wedge_lob",
    "lob-wedge": "wedge_lob",
    "putter": "putter",
}
_PATTERNS = [
    (re.compile(r"^(\d+)\s*wood$"), "wood_{}"),
    (re.compile(r"^(\d+)\s*hybrid$"), "hybrid_{}"),
    (re.compile(r"^hybrid\s*(\d+)$"), "hybrid_{}"),
    (re.compile(r"^(\d+)\s*iron$"), "iron_{}"),
    (re.compile(r"^eisen\s*(\d+)$"), "iron_{}"),
]


def canonical_club(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip().lower()
    if key in _FIXED:
        return _FIXED[key]
    for pattern, template in _PATTERNS:
        m = pattern.match(key)
        if m:
            return template.format(m.group(1))
    return None


# Reihenfolge für Anzeige/Gapping (lang -> kurz); unbekannte Typen ans Ende.
_ORDER = ["driver"]
_ORDER += [f"wood_{n}" for n in (3, 4, 5, 7, 9)]
_ORDER += [f"hybrid_{n}" for n in range(2, 8)]
_ORDER += [f"iron_{n}" for n in range(2, 10)]
_ORDER += ["wedge_pitching", "wedge_gap", "wedge_sand", "wedge_lob", "putter"]


CANONICAL_CLUBS = tuple(_ORDER)


def club_sort_key(club_type: str | None) -> int:
    if club_type in _ORDER:
        return _ORDER.index(club_type)
    return len(_ORDER)
