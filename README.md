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


## Weboberfläche

    uv run shotlake serve            # http://127.0.0.1:8000, nur lokal, ohne Anmeldung

Seiten: Übersicht (Differential-Verlauf mit gleitendem Durchschnitt, Strokes Gained, letzte Runden),
Runden, Rundendetail (Löcher, Schläge, Eingaben wie Handicap-Relevanz/PCC/AGS), Schläger, Quellen.
Einheiten m/yd und Hell/Dunkel sind umschaltbar. Der Server bindet standardmäßig nur an `127.0.0.1`;
er hat keine Anmeldung und ist nicht für das offene Netz gedacht.
