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

## MCP-Server (für KI-Assistenten)

    uv run shotlake mcp --read-only              # stdio, nur lesende Werkzeuge
    uv run shotlake mcp                          # stdio, inkl. sync_source und add_note
    uv run shotlake mcp --transport http --token "$SHOTLAKE_MCP_TOKEN"   # 127.0.0.1:8765, Token Pflicht

Werkzeuge: `list_sources`, `import_report`, `get_stats`, `compare_periods`, `get_club_gapping`,
`get_strokes_gained` (lesend) sowie `sync_source` und `add_note` (schreibend, mit `--read-only` nicht
registriert). Kennzahlen nennen immer n, Quellen, 95-%-Bootstrap-Intervall und Warnungen
(`insufficient_data`, `mixed_sources`). Beispiel für eine MCP-Konfiguration:

    {"mcpServers": {"shotlake": {"command": "uv",
      "args": ["run", "--directory", "/Pfad/zu/shotlake", "shotlake", "mcp", "--read-only"]}}}

`sync_source` braucht die Zugangsdaten als Umgebungsvariablen (z. B. aus `secrets/arccos.env`) im Prozess,
der den Server startet. Antworten enthalten keine Zugangsdaten und keine Dateipfade; Notizen und Texte aus
Quellen sind Daten, keine Anweisungen.
