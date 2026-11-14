# Proposal: Shotlake

## Why
Golfdaten liegen verteilt in fünf Systemen mit unterschiedlichen Formaten, Einheiten und Messmethoden. Entwicklung über die Zeit ist nicht erkennbar, Quellen sind nicht vergleichbar, Runden doppeln sich (z. B. Garmin + Arccos). Ziel: eine lokale, eigene Datenbasis mit belastbaren Auswertungen und einem Frage-Workflow („Hol die neue Trackman-Course-Simulation – hat sich etwas verbessert?").

## What Changes
Neues System aus: Connectors → raw → staging → canonical → marts, Analytics (Distanz/Streuung, Strokes Gained, Score Differential, Trends/Insights), Web-Frontend, MCP-Server, Betrieb (Docker/K8s, Backup, Observability).

## Capabilities (neu)
| Capability | Inhalt |
|---|---|
| ingest-connectors | Connector-Interface, 5 Quellen, Sync-Cursor, Retry, Drift-Erkennung, ToS-Doku |
| canonical-model | Schichten, Entitäten, Einheiten, Provenienz, NULL-Regel |
| dedup | Matching über Quellen, Idempotenz, manuelle Korrektur |
| analytics-distance | Länge, Dispersion, Gapping, Kalibrierung |
| analytics-sg | Strokes Gained mit konfigurierbarer Baseline |
| analytics-handicap | Score, AGS, Score Differential (offizielle Formel), Handicap-Relevanz je Runde; kein Handicap-Index |
| analytics-trends | Rolling Windows, Periodenvergleich, CI, Mindest-n, Insights |
| frontend | Dashboard, Detailseiten, Sync-UI, A/B, Notizen |
| mcp-server | 8 Tools, read-only vs. schreibend |
| ops | Secrets, Backup/Export, Logs, Datenqualität, Tests, Deployment |

## Nicht-Ziele
Multi-User, Cloud-Hosting, offizielle Partner-APIs, Social-/Leaderboard-Funktionen, Swing-Videoanalyse.

## Risiken
Inoffizielle APIs (Garmin, Arccos, Trackman Web) können brechen oder ToS verletzen → Datei-Import als Fallback je Quelle. BeBrassie-Exportweg unbekannt (Phase 0). Tour-Baselines für SG sind urheberrechtlich/lizenzseitig zu klären.

Siehe `design.md`, `tasks.md`, `open-questions.md`.
