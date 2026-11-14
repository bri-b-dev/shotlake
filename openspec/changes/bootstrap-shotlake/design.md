# Design: Shotlake

## Stack-Entscheidung (bestätigt 2026-11-14: DuckDB, Docker Compose lokal/NAS, eigenständig)
| Bereich | Wahl | Begründung |
|---|---|---|
| Sprache | Python 3.12 | `python-garminconnect`, pandas/polars/scipy/statsmodels, FIT-Parser, MCP-SDK vorhanden |
| Speicher | **DuckDB** (eine Datei) + Parquet für raw/Export | Single-User, analytische Last (Aggregate, Rolling Windows), Parquet-Export nativ, keine DB-Server-Ops. Alternative: Postgres, wenn Mehrprozess-Schreibzugriff oder K8s-Replikas nötig |
| API | FastAPI + Pydantic | Typisiert, OpenAPI, gleiche Service-Schicht wie MCP |
| Frontend | Server-gerendert (Jinja) + htmx + Alpine, Charts mit Observable Plot/uPlot | Klein, schnell, mobil, kein Build-Schritt, Dark/Light per CSS-Variablen |
| MCP | Offizielles Python-SDK, stdio + Streamable HTTP | Ruft dieselbe Service-Schicht, keine Logik-Duplikate |
| Jobs | In-Process-Scheduler (APScheduler) + CLI | Reicht für Single-User; Sync ist idempotent |
| Pakete | uv, ruff, pytest, mypy | |

**Streamlit**: nur als Wegwerf-MVP für Phase 1 denkbar; nicht als Zielarchitektur (schwache Mobil-UX, Rerun-Modell, begrenzte Gestaltung, Kopplung von UI und Logik).

**DuckDB-Einschränkung**: ein Schreiber pro Datei → API-Container ist einziger Writer, K8s: `replicas: 1`, `Recreate`-Strategie, PVC. MCP-Server und CLI sprechen über die API oder öffnen read-only.

## Schichten
```
Connector → raw (Parquet/Originaldatei, unveränderlich, content-hash, Version)
         → staging (pro Quelle geparst, quellennahe Spalten)
         → canonical (einheitliches Modell, SI, Provenienz)
         → marts (Aggregate/Views für Analytics & UI)
```
Reprocessing: staging/canonical/marts sind aus raw jederzeit neu aufbaubar.

## Service-Schicht
`services/{sync,query,analytics,notes}` – API, MCP und CLI sind dünne Adapter.

## Wichtige Entscheidungen
1. Inkrementeller Cursor je Quelle + Content-Hash je Record → Idempotenz.
2. Messmethode (radar/camera/GPS/sensor/sim) ist erstklassiges Attribut jedes Shots; Analytics trennen danach.
3. Statistik: Konfidenzintervalle per Bootstrap (nicht-parametrisch, kleine n), Mindest-n konfigurierbar.
4. Baseline-Tabellen für SG als versionierte Daten-Dateien mit Quellenangabe.
5. Trackman-Web-API und Arccos-API nur optional, abschaltbar per Config; Datei-Import bleibt Primärweg.
