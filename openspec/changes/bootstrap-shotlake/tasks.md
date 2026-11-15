# Tasks (Phasen)

## Phase 0 – Recherche & Beispielexporte
Vorab ausgewertet: `phase0-findings.md` (aus `../analytics-platform`, `../arccos-fetch`).
- [x] 0.0 Entscheidung Verhältnis zu `analytics-platform`: **A (eigenständig)**, 2026-11-14
- [ ] 0.1 BeBrassie: interne Web-API (aus HAR bekannt) gegen Live verifizieren (Token-Feld, Listen-Reihenfolge); anonymisierte Beispielrunde als Fixture erzeugen (liegt noch nicht vor); `docs/sources/bebrassie.md`; manueller Import als Fallback definieren
- [ ] 0.2b Arccos neues Dashboard (HAR liegt vor, `phase0-findings.md` §2b): Auth: Schritt 1 belegt (`POST /api/login` magic-link), Schritt 2 (Redirect-Kette aus `login2`-HAR dokumentiert) live verifizieren: Set-Cookie-Name/Laufzeit, Einmaligkeit des Tokens, Gültigkeit von `accessKey/secret` und ob `GET /magic-link?accessKey&secret&userId` eine neue Session erzeugt (alte API ist abgeschaltet, Open Question 0d entschieden: Option iv, Ablage per sops); Stabilität der `_app/remote/<hash>/…`-Hashes prüfen, devalue-Decoder als Parser, alte API `api.arccosgolf.com` gegenprüfen; anonymisierte Fixture je Funktion (`roundsBundle`, `roundDetailBundle`, `clubProfileV2`)
- [ ] 0.2 (teils überholt durch 0.2b) Arccos: echte URL des Detail-Endpoints (Antwort mit `holes[].shots[]` liegt vor, aber `docs.md` nennt dafür die Stats-URL) und des Clubs-Endpoints per Netzwerk-Tab ermitteln; Bedeutung von `userStartTerrainOverride` klären (Lie-Ableitung); Delta über `lastModifiedTime`/`roundVersion` prüfen; `docs/sources/arccos.md`
- [x] 0.2a Arccos: Beispiel-Runden (61 Stück) und `clubs.json` vorhanden – Fixtures anonymisieren (userId, Klarnamen, GPS-Heimnähe)
- [x] 0.5a Mevo+: 12 Beispiel-CSVs vorhanden (2025/2026-Schemavarianten) – als Fixtures übernehmen
- [x] 0.3 (Struktur live geprüft 2026-11-14, siehe `phase0-findings.md` §4b; Einheiten und Lie-Abdeckung an einer Runde bestimmt; offen: weitere Runden gegenprüfen, Putt-Distanz-Ableitung, Anonymisierung der Rohdaten in `probe-output/`) Garmin **Platz (API)**: python-garminconnect live testen (`get_golf_summary/scorecard/shot_data/club_stats`), tatsächliche Schlagfelder und MFA-/Token-Verhalten dokumentieren, anonymisierte Beispielrunde sichern. FIT/Export entfällt
- [x] 0.3a Garmin **Range (CSV)**: 8 Beispiel-CSVs in `/Volumes/golf/sessions` ausgewertet (`phase0-findings.md` §4a); als Fixtures kopieren und `Spieler` anonymisieren; Annahmen bestätigen (Spin-Einheit rpm, Zeitzone, 24-h-Stunde mit PM-Suffix)
- [ ] 0.3b Arccos-Alternative prüfen: offizielle On-Course-Data-API (OAuth2, Swagger `api.arccosgolf.com/swagger`) – Zugang/Antrag und Bedingungen klären
- [ ] 0.3c sops/age einrichten (noch nicht installiert) und Schlüssel-Backup festlegen
- [~] 0.4 Trackman (2026-11-14: Connector gebaut, Live-Lauf steht aus): **kein Performance Studio** → Web-API (GraphQL) ist der Primärweg. Schema/Abfrage für `session`-Detail (Schläge) selbst schreiben; Grundlage: Fix-Branch `fix/placeholder-token-and-session-query` (Alias `strokeTime`), Befunde in `phase0-findings.md` §4c; mit dem Fix-Stand des Plugins live gegenprüfen (geladenes Plugin ist vermutlich noch ungefixt); Token-Fluss und -Laufzeit dokumentieren (Skill `trackman-api-discovery`), anonymisierte Fixtures je Aktivitätsart (`SIMULATOR`, `COURSE_PLAY`, `VIRTUAL_RANGE`, `SHOT_ANALYSIS`) sichern; Einheiten (SI?) bestätigen
- [ ] 0.5 Mevo+: myflightscope-CSV-Export mit Beispielen
- [ ] 0.6 Anonymisierte Beispielexporte als Fixtures unter `tests/fixtures/<source>/`
- [x] 0.7 Bestehende „handycap"-App gesichtet (`phase0-findings.md` §4e): Abweichungen PCC-Skalierung, 26,5-Bremse, Mindestanzahl; Entscheidungen offen (Open Question 12a)
- [ ] 0.8 Offene Fragen (`open-questions.md`) mit dem Nutzer klären

## Phase 1 – Canonical-Modell + Range-Quellen
- [x] 1.1 Repo-Setup (git, uv, pytest, ruff), DuckDB-Schema (raw_object, import_run, session, shot). **Offen:** eigene staging-Schicht (Parser schreiben aktuell direkt nach canonical), Rebuild aus raw
- [x] 1.2 Connector-Interface (`Connector`: id, mode, risk, discover, read_header, parse, contract_check) mit Registry; Parser für **Mevo+-CSV** und **Garmin-Range-CSV** (Trackman-CSV entfällt: kein Performance Studio); Cursor für Datei-Quellen = Inhalts-Hash (`sync_location`); Contract-Tests je Connector (Fixtures, Invarianten, Drift: fehlende Spalte/geänderte Einheit = Fehler, neue Spalte = Warnung). **Offen für API-Connectors:** `fetch(cursor)`, Auth, Retry/Backoff
- [x] 1.3 Import-Report (neu/geändert/unverändert/Duplikat/Drift), Idempotenztests, Plausibilitäts-Flags, Provenienz (raw_hash, Importlauf)
- [~] 1.4 analytics-distance: Median/P10/P90/Streuung/Bias, Quelle- und Methodentrennung, Filter (Datum, indoor/outdoor), Gapping. **Offen:** 2D-Streuellipse, Kalibrierungsfaktor, Konfidenzintervalle
- [~] 1.5 CLI (`shotlake import|clubs|gapping`). **Offen:** FastAPI

## Phase 2 – Platzquellen, Dedup, Score/Handicap
- [ ] 2.1 Garmin, Arccos, BeBrassie (oder Fallback)
- [ ] 2.2 Course-Stammdaten (Rating/Slope/Tees)
- [x] 2.3 Dedup-Matching + manuelle Korrektur (2026-11-14: Garmin, Arccos, BeBrassie; Trackman folgt mit seinem Connector; Konfiguration `config/dedup.toml`)
- [ ] 2.4 Score, FIR/GIR/Scrambling/Putts, AGS und Score Differential (offizielle PCC-Skalierung, 9-Loch-Formel) mit Golden-Tests; Handicap-Relevanz-Kennzeichen (Vorschlag + Nutzerentscheidung); kein Handicap-Index

## Phase 3 – Strokes Gained
- [ ] 3.1 Baseline-Tabellen + Config
- [ ] 3.2 SG-Berechnung, Abdeckungsprüfung, Golden-Tests

## Phase 4 – Frontend, MCP, Insights
- [~] 4.1 Frontend-Seiten, Dark/Light, mobil (erster Durchstich 2026-11-14: Übersicht mit Differential-Verlauf und SG, Runden, Rundendetail mit Eingaben, Schläger, Quellen; offen: Streuungs-Scatter/Ellipse, Periodenvergleich, Notizen, Dedup-Review, Sync-Knopf/Upload, Konfidenzintervalle)
- [x] 4.2 MCP-Server (8 Tools, stdio + HTTP mit Token, `--read-only`; 2026-11-14)
- [~] 4.3 Trends, Periodenvergleich, Insights (Bootstrap-CI, Kennzahlenkatalog und Zeitraumvergleich über MCP vorhanden; Insights regelbasiert über MCP, CLI und Weboberfläche; Notizen als Marker mit Vorher/Nachher-Vergleich und A/B-Vergleichsseite)
- [ ] 4.4 Backup/Restore, Export, Observability, K8s-Manifeste
