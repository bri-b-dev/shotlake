# Shotlake

Persönliche Golf-Datenplattform (Single-User, lokal). Spezifikation: `openspec/changes/bootstrap-shotlake/`.

## Entwicklung

```bash
uv sync
uv run pytest        # Tests
uv run ruff check .  # Lint
```

## Nutzung (Phase 1: Range-Quellen)

```bash
uv run shotlake import mevo /pfad/zu/mevo-csvs/            # FlightScope Mevo+ CSV
uv run shotlake import garmin_range /pfad/zu/sessions/     # Garmin-Range-CSV
uv run shotlake clubs --outdoor --min-n 10                  # Länge/Streuung je Schläger
uv run shotlake gapping --outdoor                           # Gapping
```

Daten liegen unter `./data` (DB `shotlake.duckdb`, unveränderte Importdateien in `raw/`), nicht im Repo.
Mit `--units yd` werden Yards angezeigt. Wiederholte Importe sind idempotent.
