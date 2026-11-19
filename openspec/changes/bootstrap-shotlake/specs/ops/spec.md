# ops

## ADDED Requirements

### Requirement: Secrets
Secrets/Tokens SHALL niemals im Repo, in Logs, Raw-Daten oder Fehlermeldungen stehen; statische Secrets (Quell-Credentials) SHALL in einer mit **sops** (age) verschlüsselten Datei liegen und erst zur Laufzeit entschlüsselt werden; der age-Schlüssel SHALL außerhalb des Repos liegen (Dateirechte 0600 oder Docker/K8s Secret). Veränderliche Zustände (Sessions, Access-Tokens, Sync-Cursor) SHALL getrennt im Datenvolumen liegen, nicht in der sops-Datei. Klartext-Secrets SHALL NOT in Export, Backup-Klartext oder Konfigurationsdump erscheinen (Details offen: Frage 8); Token-Refresh SHALL automatisch erfolgen, Refresh-Tokens SHALL verschlüsselt bzw. mit Dateirechten 0600 gespeichert sein. Ein Pre-Commit/CI-Secret-Scan SHALL existieren.

#### Scenario: Token abgelaufen
- WHEN ein Access-Token abläuft
- THEN wird es mit Refresh-Token erneuert; schlägt das fehl, Status `auth_required`

#### Scenario: sops-Datei
- WHEN die Anwendung startet und der age-Schlüssel fehlt
- THEN startet sie ohne die betroffenen Connectors (Status `auth_required`/`secrets_unavailable`) und meldet klar, welcher Schlüssel fehlt, ohne Secret-Werte auszugeben

#### Scenario: Log-Ausgabe
- WHEN ein Request geloggt wird
- THEN sind Authorization-Header und Passwörter maskiert

### Requirement: Daten bleiben lokal
Das System SHALL keine Golfdaten an Dritte senden außer an die jeweilige Quelle beim Sync; es SHALL keine Telemetrie enthalten. Screenshot-/LLM-Extraktion SHALL nur mit expliziter Freigabe je Import erfolgen.

#### Scenario: Offline-Betrieb
- WHEN keine Internetverbindung besteht
- THEN funktionieren Analysen und Datei-Import vollständig

### Requirement: Backup und Restore
Es SHALL Befehle (`backup`, `restore`, `backup-verify`, `backup-key`) und eine Funktion in der Oberfläche für Backup und Restore geben, mit Konsistenzprüfung und dokumentiertem, getestetem Ablauf.

Das Archiv SHALL enthalten: einen konsistenten Schnappschuss der Datenbank, `raw/`, `inbox/`, die verschlüsselte Zugangsdaten-Datei und ein Manifest mit Prüfsummen. Es SHALL niemals enthalten: den Schlüssel der Zugangsdaten, das Passwort der Oberfläche, Tokens (Garmin) und Caches. Es SHALL optional mit einem Passwort verschlüsselt werden können (nie als Kommandozeilenargument).

Die Wiederherstellung SHALL vor jeder Änderung Format, Pfade (keine absoluten Pfade, kein `..`, keine Verknüpfungen), Manifest, Größen und Prüfsummen prüfen, den vorherigen Stand sichern und bei einem Fehler zurückrollen. Schlüssel, Passwort und Tokens SHALL unangetastet bleiben; gesicherte Zugangsdaten SHALL nur übernommen werden, wenn der vorhandene Schlüssel sie lesen kann.

#### Scenario: Restore
- WHEN ein Backup auf leerer Instanz eingespielt wird
- THEN liefern Analysen identische Ergebnisse wie vorher

#### Scenario: Manipuliertes Archiv
- WHEN ein Archiv einen Pfad mit `..`, eine Verknüpfung, eine falsche Prüfsumme oder einen Eintrag außerhalb des Manifests enthält
- THEN wird die Wiederherstellung abgelehnt und nichts geändert

#### Scenario: Schlüssel getrennt
- WHEN eine Sicherung erstellt wird
- THEN enthält sie den Schlüssel nicht, und `backup-key` gibt ihn zum getrennten Sichern aus

### Requirement: Gesamt-Export
Es SHALL einen Export aller Tabellen (Canonical, Eingaben, Notizen, Herkunft) als Parquet und CSV geben, per Befehl (`export`) und als ZIP-Download in der Oberfläche. Er SHALL ein `schema.json` mit Format- und Programmversion, Konventionen (Zeit, Einheiten, Kodierung), je Tabelle Beschreibung, Spalten mit Typ, Nullbarkeit und Einheit, Primärschlüssel, Zeilenzahl sowie Datei und SHA-256 enthalten. Der Export SHALL einen einheitlichen Stand der Datenbank lesen, deterministisch sein und weder Zugangsdaten noch Schlüssel enthalten. Jede Tabelle der Datenbank SHALL eine Beschreibung haben.

#### Scenario: Export
- WHEN `export` läuft
- THEN entstehen Dateien je Tabelle in `parquet/` und `csv/` mit `schema.json` und Versionsangabe

#### Scenario: Vollständigkeit
- WHEN die Zeilenzahlen von Datenbank, Parquet, CSV und `schema.json` verglichen werden
- THEN stimmen sie für jede Tabelle überein

### Requirement: Deployment
Das System SHALL als Docker-Image (non-root, ein Datenvolumen `/data`, Health-Check) mit docker-compose-Beispiel (nur lokal gebunden, schreibgeschütztes Wurzeldateisystem, ohne zusätzliche Rechte) bereitgestellt werden. Das Image SHALL weder Daten noch Zugangsdaten enthalten. Ein CI-Workflow SHALL Tests ausführen und das Image für amd64 und arm64 nach ghcr.io veröffentlichen (nur bei Pushes auf `main` und Versions-Tags, nie bei Pull Requests, mit minimalen Rechten). K8s-Manifeste/Helm (PVC, Secret, ConfigMap, Probes, `replicas: 1` bei DuckDB) SHALL optional bereitgestellt werden. `/healthz` (Liveness) und `/readyz` (Bereitschaft) SHALL ohne Anmeldung erreichbar sein und keine Pfade oder Zugangsdaten nennen.

#### Scenario: Neustart
- WHEN der Container neu startet
- THEN bleiben Daten und Sync-Cursor erhalten

#### Scenario: Erster Start
- WHEN das Image mit leerem Datenvolumen startet
- THEN wird die Standardkonfiguration nach `/data/config` kopiert, vorhandene Dateien bleiben unverändert, und die Oberfläche fragt nach dem Passwort

### Requirement: Observability
Sync-Läufe SHALL strukturiert geloggt werden (Run-ID, Quelle, Dauer, Zähler, Fehler); Health-Endpunkt und optional Prometheus-Metriken SHALL vorhanden sein.

#### Scenario: Fehlgeschlagener Sync
- WHEN ein Sync scheitert
- THEN ist Fehlerursache über Run-ID in Log und UI auffindbar

### Requirement: Datenqualitäts-Checks
Nach jedem Import SHALL Checks laufen: Ausreißer, fehlende Pflicht-/Wunschfelder (Quote je Feld/Quelle), Duplikatverdacht, Einheiten-Plausibilität; Ergebnis SHALL im Import-Report erscheinen.

#### Scenario: Fehlende Felder
- WHEN 40 % der Schläge keine Seitenabweichung haben
- THEN wird das im Report als Warnung ausgewiesen

### Requirement: Tests
Die Testsuite SHALL umfassen: Parser-Tests mit Fixtures je Quelle, Contract-Tests (Schema-Drift), Golden-Tests für SG und Score Differential, Idempotenztests (Doppelimport), Dedup-Tests, Einheiten-/Zeitzonentests.

#### Scenario: CI
- WHEN ein Pull Request geöffnet wird
- THEN laufen alle Tests und der Secret-Scan

### Requirement: ToS-/Stabilitätsdokumentation
Je inoffizieller Schnittstelle SHALL `docs/sources/<source>.md` Risiko, Nutzungsbeschränkungen (Rate, nur eigener Account), Abschaltschalter und Fallback enthalten.

#### Scenario: Abschalten
- WHEN `connectors.garmin.enabled=false`
- THEN wird nicht synchronisiert; vorhandene Daten bleiben nutzbar
