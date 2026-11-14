"""Dekoder für das `devalue`-Format (SvelteKit Remote Functions, JSON mit indizierten Referenzen).

Eingabe ist der JSON-String (oder bereits geparst) der Form `[root, ...]`, in dem jedes Element ein
Primitivwert, ein Array von Indizes, ein Objekt (Schlüssel -> Index) oder ein getaggtes Array
(`["Date", iso]`, `["Set", ...]`, `["Map", ...]`, `["null", ...]`, `["BigInt", s]`) ist.
Sonderindizes: -1 undefined, -2 Lücke, -3 NaN, -4 +Inf, -5 -Inf, -6 -0. Zyklische Referenzen
werden unterstützt. Unbekannte Tags ergeben `{"$type": tag, "value": ...}` statt eines Fehlers.
"""

from __future__ import annotations

import json
import math
from typing import Any

UNDEFINED = None  # undefined wird wie null behandelt
_SPECIAL: dict[int, float | None] = {
    -1: UNDEFINED,
    -3: math.nan,
    -4: math.inf,
    -5: -math.inf,
    -6: -0.0,
}
_HOLE = -2


class DevalueError(ValueError):
    pass


def parse(data: str | list[Any]) -> Any:
    values: Any = json.loads(data) if isinstance(data, str) else data
    if not isinstance(values, list) or not values:
        raise DevalueError("devalue: nicht-leeres Array erwartet")
    cache: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index in _SPECIAL:
            return _SPECIAL[index]
        if index < 0 or index >= len(values):
            raise DevalueError(f"devalue: ungültiger Index {index}")
        if index in cache:
            return cache[index]
        value = values[index]
        if not isinstance(value, list | dict):
            cache[index] = value
            return value
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                return _tagged(index, value)
            out_list: list[Any] = []
            cache[index] = out_list
            for ref in value:
                out_list.append(None if ref == _HOLE else hydrate(ref))
            return out_list
        out_obj: dict[str, Any] = {}
        cache[index] = out_obj
        for key, ref in value.items():
            out_obj[key] = hydrate(ref)
        return out_obj

    def _tagged(index: int, value: list[Any]) -> Any:
        tag, rest = value[0], value[1:]
        result: Any
        if tag == "Date":
            result = rest[0]  # ISO-String belassen, Aufrufer parst bei Bedarf
        elif tag == "BigInt":
            result = int(rest[0])
        elif tag == "Set":
            result = []
            cache[index] = result
            result.extend(hydrate(r) for r in rest)
        elif tag == "Map":
            result = {}
            cache[index] = result
            for k, v in zip(rest[::2], rest[1::2], strict=True):
                result[str(hydrate(k))] = hydrate(v)
        elif tag == "null":
            result = {}
            cache[index] = result
            for k, v in zip(rest[::2], rest[1::2], strict=True):
                result[k] = hydrate(v)
        else:
            result = {"$type": tag, "value": [hydrate(r) for r in rest if isinstance(r, int)]}
        cache[index] = result
        return result

    return hydrate(0)
