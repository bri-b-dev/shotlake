# Shotlake – Projektkontext

Persönliche Golf-Datenplattform (Single-User, lokal/self-hosted). Sammelt Daten aus Garmin Golf, Arccos, FlightScope Mevo+, BeBrassie und Trackman, vereinheitlicht sie und wertet sie aus (Länge, Streuung, Strokes Gained, Score, Score Differential, Trends).

## Konventionen
- Sprache: Spezifikation Deutsch, Code/Bezeichner Englisch.
- Normative Wörter: SHALL / SHALL NOT.
- Intern SI-Einheiten (m, m/s, °C, rpm), Zeitstempel UTC gespeichert, Anzeige Europe/Berlin.
- Fehlende Messwerte = NULL, nie geschätzt oder erfunden.
- Secrets nie im Repo (`.env`, Secret-Store, K8s Secrets).
- Capabilities: ingest-connectors, canonical-model, dedup, analytics-distance, analytics-sg, analytics-handicap, analytics-trends, frontend, mcp-server, ops.

## Aktive Changes
- `changes/bootstrap-shotlake` – Gesamtvorhaben inkl. Phasen 0–4 und offene Fragen.
