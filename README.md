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
- Garmin (course, needs `uv sync --extra garmin`): first login with a two-factor code in *Settings* (email and password are used
  once and not stored; only the token is, in `data/garmin/`). Reads from `~/.garminconnect` otherwise.

**Strokes Gained baselines.** Two approximate tables ship in `baselines/`: `hcp40w-approx-v1`
("Mein Niveau", the default) and `hcp18w-approx-v1` (goal handicap 18). Switch with the selector on
Overview, Round detail, Insights and Comparison (`?b=<id>`), with `--baseline <id>` on the CLI
(`uv run shotlake baselines` lists them) or the `baseline` argument of the MCP tools. Both are
estimates, not measured data: compare rounds, categories and periods against the same table.

**Export.** `uv run shotlake export [--out DIR] [--format parquet|csv|all] [--zip]` (or *Settings →
Export*) writes every table as Parquet and CSV plus `schema.json` (version, units, columns, row
counts, SHA-256). It holds your raw values including GPS positions, but no credentials.

**Backup and restore.** *Settings → Sicherung* downloads an archive (database snapshot, raw data,
uploads, the encrypted credentials file, checksums) and restores one by upload. The same from the
command line:

    uv run shotlake backup [--out FILE] [--password]   # --password encrypts; reads SHOTLAKE_BACKUP_PASSWORD or asks
    uv run shotlake backup-verify FILE                 # checks structure and checksums only
    uv run shotlake restore FILE [--yes]               # replaces database, raw data and uploads
    uv run shotlake backup-key [--out FILE]            # the key for secrets.enc, store it separately

A backup never contains the key, the interface password or the Garmin token, so a lost archive
reveals no access. Keep the key apart from the archive (password manager). Restoring first saves the
current state under `data/backups/` (last five kept), never touches key, password or tokens, and
adopts stored credentials only if the existing key can read them (else `secrets.enc.from-backup`).
Stop the server before a command-line `restore`.

By default the server binds only to `127.0.0.1`. When exposing it, put it behind HTTPS (the session
cookie is marked `Secure` over HTTPS). `SHOTLAKE_WEB_AUTH=off` disables the password check (only for
setups protected otherwise).

## Docker (NAS or home server)

    docker compose up -d        # uses docker-compose.yml and ghcr.io/bri-b-dev/shotlake:latest

Open `http://<host>:8000` and set the interface password on first start. Everything that persists
lives in one volume (`/data`: database, raw data, uploads, credentials, configuration, backups). The
image runs as user 10001 without root, with a read-only root filesystem; with a bind mount instead of
a volume run `chown 10001:10001 data` first. On first start the default configuration is copied to
`/data/config` and kept there (edit it, or use *Settings*).

Port: by default the compose file publishes `127.0.0.1:8000`, reachable only on the host itself. On a
NAS set `SHOTLAKE_BIND=0.0.0.0` (all interfaces) and, if 8000 is taken, `SHOTLAKE_PORT=8001`; then open
`http://<nas-ip>:8001`. A binding with `127.0.0.1` is never reachable from other devices. Over the
network the password and session cookie travel unencrypted: use a trusted LAN or put a reverse proxy
with TLS in front. Set `SHOTLAKE_SECRET_KEY` to keep the key of the stored credentials outside the
volume. Update with `docker compose pull && docker compose up -d`.

DuckDB version: Shotlake is pinned to DuckDB 1.x (`duckdb>=1.4,<2`). The Linux build of DuckDB 2.0
needs AVX2 and crashes with "Illegal instruction" on processors without it (many NAS devices, for
example Celeron J4025; tested). To move data between machines use *Backup and restore*: the archive
holds the database in the DuckDB 1.0 file format, readable by every version. A database file written
by DuckDB 2.x cannot be opened by 1.x; copy it first with
`uv run --no-project --with "duckdb==2.0.0" python scripts/migrate_duckdb.py data/shotlake.duckdb --replace`
(stop Shotlake first; the original stays as `shotlake.duckdb.original`).

Health checks (no login): `/healthz` (process alive) and `/readyz` (data directory writable,
database readable; `busy` while a backup runs). The image's `HEALTHCHECK` uses `/healthz`.

The GitHub workflow `.github/workflows/ci.yml` runs lint, types and tests, then builds the image for
`linux/amd64` and `linux/arm64` and pushes it to `ghcr.io/<owner>/shotlake` on every push to `main`
(tags `latest`, `main`, short commit) and on version tags `v*` (`1.2.3`, `1.2`). Pull requests only
build. Build locally with `docker build -t shotlake .`.

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
