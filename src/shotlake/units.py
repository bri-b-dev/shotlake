"""Einheiten-Umrechnung (intern SI) und tolerantes Parsen von Quellwerten."""

from __future__ import annotations

import re

YD_TO_M = 0.9144
MPH_TO_MS = 0.44704
FT_TO_M = 0.3048

_MISSING = {"", "-", "- *", "n/a", "na", "none", "null"}
_NUMBER = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")


def yd_to_m(v: float) -> float:
    return v * YD_TO_M


def mph_to_ms(v: float) -> float:
    return v * MPH_TO_MS


def kmh_to_ms(v: float) -> float:
    return v / 3.6


def ft_to_m(v: float) -> float:
    return v * FT_TO_M


def f_to_c(v: float) -> float:
    return (v - 32.0) * 5.0 / 9.0


def is_missing(text: str | None) -> bool:
    return text is None or text.strip().lower() in _MISSING


def parse_number(text: str | None) -> float | None:
    """Zahl am Anfang lesen ("77 °F" -> 77.0). Fehlwerte ("-", "- *", "N/A", "") -> None."""
    if is_missing(text):
        return None
    m = _NUMBER.match(text.replace(",", "."))  # type: ignore[union-attr]
    return float(m.group(1)) if m else None


def parse_signed(text: str | None) -> float | None:
    """Zahl mit Richtungssuffix: "3.8 R" -> +3.8, "1.2 L" -> -1.2 (rechts positiv)."""
    value = parse_number(text)
    if value is None:
        return None
    t = text.strip().upper()  # type: ignore[union-attr]
    if t.endswith("L"):
        return -abs(value)
    if t.endswith("R"):
        return abs(value)
    return value
