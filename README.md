# Shotlake

Personal golf data platform (single-user, local). Specification: `openspec/changes/bootstrap-shotlake/`.

## Development

```bash
uv sync
uv run pytest        # Tests
uv run ruff check . # Lint
```

## Usage (Phase 1: Range Sources)

```bash
uv run shotlake import mevo /path/to/mevo-csvs/            # FlightScope Mevo+ CSV
uv run shotlake import garmin_range /path/to/sessions/     # Garmin Range CSV
uv run shotlake clubs --outdoor --min-n 10                  # Distance/dispersion per club
uv run shotlake gapping --outdoor                           # Gapping
```

Data is stored in `./data` (database `shotlake.duckdb`, original import files in `raw/`) and is not included in the repository.
Use `--units yd` to display values ​​in yards. Repeated imports are idempotent.

## Web Interface

uv run shotlake serve            # http://127.0.0.1:8000

Pages: Overview (differential trend with moving average, Strokes Gained, recent rounds),
Rounds, Round Details (holes, shots, inputs such as handicap relevance/PCC/AGS), Clubs (distance,
gapping, dispersion scatter with 95% ellipse), Insights, Notes, Duplicates, Sources, Settings.
Units (m/yd) and themes (light/dark) can be toggled.

**Operating without a terminal.** On first start the interface asks for a password (at least
10 characters; scrypt hash in `data/auth.json`, session cookie, rate-limited login). After that:

- *Settings*: enter credentials for BeBrassie, Arccos and Trackman. They are stored encrypted
  (Fernet) in `data/secrets.enc`; set `SHOTLAKE_SECRET_KEY` to keep the key outside the data
  directory, otherwise `data/secret.key` (0600) is created. Stored values are never shown again.
  Environment variables `SHOTLAKE_<SOURCE>_<FIELD>` take precedence.
- *Sources*: sync buttons (incremental or full), "check" for credentials, CSV upload for Mevo+ and
  Garmin Range. Syncs run in the background.
- Garmin (course): first login with a two-factor code in *Settings* (email and password are used
  once and not stored; only the token is, in `data/garmin/`). Reads from `~/.garminconnect` otherwise.

By default the server binds only to `127.0.0.1`. When exposing it, put it behind HTTPS (the session
cookie is marked `Secure` over HTTPS). `SHOTLAKE_WEB_AUTH=off` disables the password check (only for
setups protected otherwise).

## MCP Server (for AI Assistants)

uv run shotlake mcp --read-only              # stdio, read-only tools
uv run shotlake mcp                          # stdio, includes `sync_source` and `add_note`
uv run shotlake mcp --transport http --token "$SHOTLAKE_MCP_TOKEN"   # 127.0.0.1:8765, token required

Tools: `list_sources`, `import_report`, `get_stats`, `compare_periods`, `get_club_gapping`,
`get_strokes_gained` (read-only), as well as `sync_source` and `add_note` (write-enabled; not
registered when using `--read-only`). Metrics always include the sample size (n), data sources,
a 95% bootstrap confidence interval, and any warnings (e.g., `insufficient_data`, `mixed_sources`).
Example of an MCP configuration:

{"mcpServers": {"shotlake": {"command": "uv",
"args": ["run", "--directory", "/path/to/shotlake", "shotlake", "mcp", "--read-only"]}}}

`sync_source` requires access credentials to be provided as environment variables (e.g., from
`secrets/arccos.env`) within the process that launches the server. Responses do not contain
credentials or file paths; notes and text extracted from sources are treated as data, not
as instructions. 

## Dedup (dieselbe Runde in mehreren Quellen)

    uv run shotlake dedup                 # abgleichen, verknüpfte Runden und Vorschläge zeigen
    uv run shotlake dedup accept-all      # alle offenen Vorschläge bestätigen
    uv run shotlake dedup confirm A B     # zwei Runden als dieselbe festlegen (auch zusammenführen)
    uv run shotlake dedup reject A B      # zwei Runden als verschieden festlegen (trennt)

Regeln, Platz-Aliasse (z. B. alter und neuer Clubname) und die führende Quelle je Feldgruppe stehen in
`config/dedup.toml`. Automatisch verknüpft wird nur bei eindeutigem Treffer mit gleichem Club, ähnlicher
Startzeit, gleicher Lochzahl und gleichem Score; alles andere ist ein Vorschlag. Entscheidungen bleiben
bei jedem Abgleich bestehen. Nach `import`, `sync` und `reprocess` läuft der Abgleich automatisch. Ohne
`--source` zählen alle Auswertungen jede Runde einmal (`--include-duplicates` schaltet das ab). Die
Weboberfläche hat dafür die Seite „Dubletten".
