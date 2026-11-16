# Tasks (Phasen)

## Phase 0 – Recherche & Beispielexporte
Vorab ausgewertet: `phase0-findings.md` (aus `../analytics-platform`, `../arccos-fetch`).
- [x] 0.0 Entscheidung Verhältnis zu `analytics-platform`: **A (eigenständig)**, 2026-11-14
- [~] 0.1 BeBrassie: Live-Sync läuft (2026-11-14: Login, Rundenliste und `show-round` funktionieren, die Listen-Reihenfolge wird nicht ausgenutzt; reine Score-Runden liefern HTTP 400 und werden als Kopfzeilen-Runden importiert). **Offen:** anonymisierte Beispielrunde als Fixture, `docs/sources/bebrassie.md`, manueller Import als Fallback
- [~] 0.2b Arccos neues Dashboard: Live-Sync läuft (2026-11-14: 60 Runden, 3339 Schläge; Hash-Entdeckung aus dem JS-Bundle, devalue-Decoder, Credentials aus HAR per Skript). **Offen:** Laufzeit von `accessKey/secret` und Session, Einmaligkeit des Magic-Link-Tokens, Rotation der Zugangsdaten und HAR-Dateien löschen, anonymisierte Fixtures je Funktion
- [x] 0.2 (überholt durch 0.2b) Arccos: Detail-Endpoint und Terrain-Werte sind über das neue Dashboard geklärt; alte API abgeschaltet
- [x] 0.2a Arccos: Beispiel-Runden (61 Stück) und `clubs.json` vorhanden – Fixtures anonymisieren (userId, Klarnamen, GPS-Heimnähe)
- [x] 0.5a Mevo+: 12 Beispiel-CSVs vorhanden (2025/2026-Schemavarianten) – als Fixtures übernehmen
- [x] 0.3 (Struktur live geprüft 2026-11-14, siehe `phase0-findings.md` §4b; Einheiten und Lie-Abdeckung an einer Runde bestimmt; offen: weitere Runden gegenprüfen, Putt-Distanz-Ableitung, Anonymisierung der Rohdaten in `probe-output/`) Garmin **Platz (API)**: python-garminconnect live testen (`get_golf_summary/scorecard/shot_data/club_stats`), tatsächliche Schlagfelder und MFA-/Token-Verhalten dokumentieren, anonymisierte Beispielrunde sichern. FIT/Export entfällt
- [x] 0.3a Garmin **Range (CSV)**: 8 Beispiel-CSVs in `/Volumes/golf/sessions` ausgewertet (`phase0-findings.md` §4a); als Fixtures kopieren und `Spieler` anonymisieren; Annahmen bestätigen (Spin-Einheit rpm, Zeitzone, 24-h-Stunde mit PM-Suffix)
- [ ] 0.3b Arccos-Alternative prüfen: offizielle On-Course-Data-API (OAuth2, Swagger `api.arccosgolf.com/swagger`) – Zugang/Antrag und Bedingungen klären
- [~] 0.3c Secrets: entschieden 2026-11-15, Zugangsdaten über die Oberfläche in verschlüsseltem Speicher (Fernet) statt sops; offen: Schlüssel-Backup (`SHOTLAKE_SECRET_KEY` bzw. `secret.key` sichern), Ablösung der `secrets/*.env` und Rotation der Arccos-Zugangsdaten
- [x] 0.4 Trackman (2026-11-14: Connector gebaut und live gelaufen: 40 Sessions mit 1199 Schlägen, 15 Sim-Runden mit 598 Platzschlägen; eigene GraphQL-Abfragen gegen die offene Schema-Introspection geprüft; Einheiten SI bestätigt). **Offen:** anonymisierte Fixtures je Aktivitätsart, `docs/sources/trackman.md`, Token-Laufzeit (ca. 7 Tage laut Referenz) beobachten
- [ ] 0.5 Mevo+: myflightscope-CSV-Export mit Beispielen
- [ ] 0.6 Anonymisierte Beispielexporte als Fixtures unter `tests/fixtures/<source>/`
- [x] 0.7 Bestehende „handycap"-App gesichtet (`phase0-findings.md` §4e): Abweichungen PCC-Skalierung, 26,5-Bremse, Mindestanzahl; Entscheidungen offen (Open Question 12a)
- [ ] 0.8 Offene Fragen (`open-questions.md`) mit dem Nutzer klären

## Phase 1 – Canonical-Modell + Range-Quellen
- [x] 1.1 Repo-Setup (git, uv, pytest, ruff), DuckDB-Schema (raw_object, import_run, session, shot). **Offen:** eigene staging-Schicht (Parser schreiben aktuell direkt nach canonical), Rebuild aus raw
- [x] 1.2 Connector-Interface (`Connector`: id, mode, risk, discover, read_header, parse, contract_check) mit Registry; Parser für **Mevo+-CSV** und **Garmin-Range-CSV** (Trackman-CSV entfällt: kein Performance Studio); Cursor für Datei-Quellen = Inhalts-Hash (`sync_location`); Contract-Tests je Connector (Fixtures, Invarianten, Drift: fehlende Spalte/geänderte Einheit = Fehler, neue Spalte = Warnung). **Offen für API-Connectors:** `fetch(cursor)`, Auth, Retry/Backoff
- [x] 1.3 Import-Report (neu/geändert/unverändert/Duplikat/Drift), Idempotenztests, Plausibilitäts-Flags, Provenienz (raw_hash, Importlauf)
- [x] 1.4 analytics-distance: Median/P10/P90/Streuung/Bias, Quelle- und Methodentrennung (Trackman-Simulator = Methode `sim`), Teilschlag-Filter (Ballgeschwindigkeit < 70 % des Schläger-Medians), Filter (Datum, indoor/outdoor), Gapping, 2D-Streuung mit 95-%-Ellipse (2026-11-15). **Offen (bewusst zurückgestellt):** Kalibrierungsfaktor, Konfidenzintervalle für Distanzen
- [x] 1.5 CLI (`import|sync|reprocess|clubs|gapping|rounds|trend|sg|course-shots|insights|dedup|serve|mcp` u. a.) und FastAPI-Oberfläche

## Phase 2 – Platzquellen, Dedup, Score/Handicap
- [x] 2.1 Garmin (API), Arccos, BeBrassie und Trackman (Sim-Runden getrennt, nicht in gemeinsamen Auswertungen)
- [~] 2.2 Course-Stammdaten: Rating/Slope/Tees je Quelle aus den Runden (BeBrassie Damen/Herren, Garmin, Trackman); Rating-Übernahme zwischen verknüpften Runden; `tee-set` für manuelle Werte. **Offen:** eigene Kurs-Stammdaten unabhängig von Runden
- [x] 2.3 Dedup-Matching + manuelle Korrektur (2026-11-14: Garmin, Arccos, BeBrassie; Trackman folgt mit seinem Connector; Konfiguration `config/dedup.toml`)
- [x] 2.4 Score, FIR/GIR/Scrambling/Putts, AGS und Score Differential (9-Loch auf 18 hochgerechnet), Handicap-Relevanz nur per Nutzerentscheidung (Standard nein), kein Handicap-Index; Golden-Tests in `tests/test_score.py`

## Phase 3 – Strokes Gained
- [x] 3.1 Baseline-Tabellen + Config (`baselines/hcp18w-approx-v1.toml`, eigene Näherung für Handicap 18 Damen, als `approximation` gekennzeichnet)
- [x] 3.2 SG-Berechnung nur über vollständig erfasste Löcher mit Grund, Kategorien tee/approach/short/green_play/putting/penalty, Tests in `tests/test_sg.py`. Sim-Runden nur gezielt (`--source trackman`). **Offen:** Lagen `Fringe`/`Rock`/`Mulch`/`Pinestraw` ohne Baseline-Wert, Garmin ohne echte Putt-Startdistanz

## Phase 4 – Frontend, MCP, Insights
- [~] 4.1 Frontend-Seiten, Dark/Light, mobil: Übersicht, Runden, Rundendetail mit Eingaben, Schläger (inkl. Streuungs-Scatter mit Ellipse), Quellen, Periodenvergleich, Notizen, Dubletten-Review vorhanden. Sync-Knopf, Prüfen, Datei-Upload, Einstellungen mit verschlüsselten Zugangsdaten und Passwortschutz (2026-11-15). **Offen:** Garmin-Erstanmeldung mit MFA in der Oberfläche, Konfidenzintervalle in allen Ansichten; Insights-Seite ca. 1,4 s (Ziel 1 s)
- [x] 4.2 MCP-Server (8 Tools, stdio + HTTP mit Token, `--read-only`; 2026-11-14)
- [x] 4.3 Trends, Periodenvergleich, Insights (Bootstrap-CI, Kennzahlenkatalog, regelbasierte Insights über MCP, CLI und Weboberfläche, Notizen als Marker mit Vorher/Nachher-Vergleich, A/B-Vergleich)
- [ ] 4.4 Backup/Restore, Export, Observability, K8s-Manifeste
