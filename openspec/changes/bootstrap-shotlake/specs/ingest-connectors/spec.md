# ingest-connectors

## ADDED Requirements

### Requirement: Gemeinsames Connector-Interface
Jede Quelle SHALL als Connector hinter einem gemeinsamen Interface implementiert sein mit: `id`, `mode` (api | file; ein Screenshot-Modus ist vorerst nicht im Umfang), `auth`, `fetch(cursor) -> RawBatch`, `parse(raw) -> StagingRecords`, `cursor_of(batch)`, `contract_check(raw)`.

#### Scenario: Neuer Connector
- WHEN ein Connector das Interface implementiert und registriert wird
- THEN erscheint er in `list_sources` und ist über `sync_source` ansprechbar, ohne Änderungen am Kern

### Requirement: Raw-Ablage unverändert und versioniert
Der Connector SHALL jede Rohantwort/-datei unverändert mit Quelle, Abrufzeit, Content-Hash und Connector-Version ablegen, bevor geparst wird.

#### Scenario: Parser-Fehler
- WHEN das Parsen fehlschlägt
- THEN bleibt das Raw-Objekt erhalten und der Lauf wird als fehlgeschlagen mit Fehlerdetail protokolliert

### Requirement: Inkrementeller Sync-Cursor
Jeder Connector SHALL einen persistierten Cursor (Zeitstempel/ID/Datei-Hash-Menge) führen und standardmäßig nur Neues holen; ein Vollabgleich SHALL per Option (`--full`) möglich sein.

#### Scenario: Zweiter Sync ohne Änderungen
- WHEN `sync_source` direkt nach einem erfolgreichen Sync erneut läuft
- THEN meldet der Import-Report 0 neu, 0 geändert und es entstehen keine Duplikate

#### Scenario: Cursor wird nur bei Erfolg vorgezogen
- WHEN ein Lauf mittendrin abbricht
- THEN bleibt der Cursor auf dem letzten vollständig verarbeiteten Stand

### Requirement: Fehler- und Retry-Verhalten
Netzwerk-/Rate-Limit-Fehler SHALL mit exponentiellem Backoff (max. konfigurierbare Versuche) wiederholt werden; Auth-Fehler SHALL NOT wiederholt werden, sondern den Status `auth_required` setzen.

#### Scenario: HTTP 429
- WHEN die Quelle 429 liefert
- THEN wartet der Connector gemäß `Retry-After`/Backoff und versucht erneut, danach Status `rate_limited`

### Requirement: Import-Report
Jeder Sync SHALL einen Import-Report erzeugen: neu / geändert / unverändert / Duplikate / Fehler / übersprungen, je Entitätstyp, mit Zeitraum und Dauer.

#### Scenario: Frage-Workflow
- WHEN der Nutzer „Hol die neue Trackman-Course-Simulation" auslöst
- THEN wird Trackman synchronisiert, der Report mit Delta angezeigt und anschließend ein Periodenvergleich (siehe analytics-trends) angeboten

### Requirement: Schema-Drift-Erkennung
Inoffizielle Connectors SHALL Antworten gegen ein erwartetes Schema prüfen; Abweichungen (fehlende/neue/typveränderte Felder) SHALL den Lauf als `drift_detected` markieren und im Log ausweisen, ohne canonical-Daten zu verfälschen.

#### Scenario: Feld entfällt
- WHEN die API ein bisher vorhandenes Pflichtfeld nicht mehr liefert
- THEN wird nichts nach canonical geschrieben, Raw bleibt gespeichert und der Status zeigt `drift_detected`

#### Scenario: Contract-Test
- WHEN die Testsuite läuft
- THEN prüft sie je Connector Fixtures gegen das Schema

### Requirement: Connector Garmin Golf
Der Garmin-Connector SHALL zwei Wege trennen: **Platzrunden ausschließlich über die API** (python-garminconnect, Modus api: `get_golf_summary`, `get_golf_scorecard`, `get_golf_shot_data`, `get_golf_club_stats`; da Löcher 10–18 nicht filterbar sind, werden stets alle Löcher geholt) und **Range-Sessions per CSV-Import** (Modus file, siehe nächste Requirement). Ein FIT-/Account-Export ist kein Bestandteil des Umfangs. Die API liefert (Live-Probe 2026-11-14) Scorecards mit Tee, `teeBoxRating` und `teeBoxSlope`, Lochwerte (Schläge, Strafschläge, Putts, Fairway-Ergebnis, Fahnenposition) sowie Schlag-für-Schlag-Daten mit Start-/End-Position, `lie`, `lieSource`, Distanz in Metern, Schläger-ID und `excludeFromStats`. Diese Felder SHALL im Staging erhalten bleiben; `excludeFromStats`, `inProgress` und `roundInProgress` SHALL berücksichtigt werden. Einheiten (an einer Runde bestimmt, weitere Runden SHALL im Contract-Test gegengeprüft werden): Positionen von Schlägen und Fahnen als ganze Zahlen in Garmin-Semicircles (Grad = Wert × 180 / 2³¹), Platzkoordinaten als Grad × 10⁶, `shotTime` und `shotTimeZoneOffset` in Millisekunden, `meters` in Metern, `startTime/endTime` ISO-8601 UTC. Werte außerhalb plausibler Bereiche SHALL als `quality_flag` markiert werden. Lie-Werte (`TeeBox`, `Fairway`, `Rough`, `Bunker`, `Green`) SHALL auf das kanonische Lie-Vokabular abgebildet und `lieSource` (`CARTOGRAPHY` automatisch, `CLIENT` manuell) erhalten werden; unbekannte Werte SHALL gemeldet statt geraten werden. `scoreType` (z. B. `STABLEFORD`) SHALL nicht mit Brutto-Schlägen verwechselt werden. Ein Token-Store (Rechte 0600) SHALL wie ein Secret behandelt werden, MFA SHALL unterstützt werden.

#### Scenario: Shot-Daten mit Lie
- WHEN ein Schlag `startLoc.lie` und `endLoc.lie` enthält
- THEN werden beide samt `lieSource` im Staging gespeichert und fehlende Lies bleiben NULL

#### Scenario: Runde in Bearbeitung
- WHEN `roundInProgress=true`
- THEN wird sie als unvollständig gespeichert und beim nächsten Sync aktualisiert, ohne in Statistiken zu zählen

#### Scenario: Neun-Loch-Runde
- WHEN `holesCompleted=9` ist, obwohl die Scorecard 18 Löcher führt
- THEN werden nur gespielte Löcher gewertet und die Runde als 9-Loch gekennzeichnet Token SHALL gecacht und erneuert werden (kein Passwort-Dauerspeicher im Klartext). Das ToS-/Stabilitätsrisiko SHALL in `docs/sources/garmin.md` dokumentiert sein.

#### Scenario: MFA
- WHEN der Login MFA verlangt
- THEN fordert der Connector den Code interaktiv/über CLI an und speichert nur Tokens

### Requirement: Garmin Range-CSV
Der Parser für Garmin-Range-CSVs (`DrivingRange-<Datum> <Zeit>.csv`) SHALL: UTF-8 mit BOM lesen; die zweite Kopfzeile als Einheitenzeile behandeln und nicht als Schlag; deutsche Spaltennamen auf kanonische Felder abbilden; Zeitstempel im Format `MM/DD/YY HH:MM:SS` mit 24-h-Stunde lesen und ein angehängtes AM/PM ignorieren; die Session-Zeit aus den Zeilen ableiten, nicht aus dem Dateinamen (dieser ist der Exportzeitpunkt); `Drehratentyp` (Gemessen/Berechnet) als `spin_source` speichern; leere Felder als NULL lesen; `Schlägerart` über eine Mapping-Tabelle auf Schlägertypen abbilden und unbekannte Werte melden statt zu raten; Spieler-Namen nicht übernehmen. Messmethode ist `radar`, Quelle Range.

#### Scenario: Zwei Kopfzeilen
- WHEN eine Datei mit Header- und Einheitenzeile importiert wird
- THEN enthält die Session nur echte Schläge und die Einheiten werden aus der zweiten Zeile geprüft (Abweichung → Drift-Warnung)

#### Scenario: Exportzeit ≠ Sessionzeit
- WHEN eine Datei im Namen `2023-10-06` trägt, ihre Schläge aber vom `08/27/23` stammen
- THEN ist die Session am 2023-08-27 datiert

#### Scenario: Mehrere Dateien eines Exportlaufs
- WHEN fünf Dateien desselben Exportlaufs importiert werden
- THEN entstehen fünf Sessions mit je eigener Session-Zeit, und ein Re-Import erzeugt keine Duplikate

#### Scenario: Unbekannter Schlägername
- WHEN `Schlägerart` einen nicht abgebildeten Wert enthält
- THEN wird der Schlag mit `club_unmapped` importiert und im Import-Report aufgeführt

### Requirement: Connector Arccos
Der Arccos-Connector SHALL nach Klärung in Phase 0 per Export oder inoffizieller API Runden- und Schlagdaten (inkl. Lie, Distanz zum Loch) importieren; die offizielle Partner-API SHALL NOT vorausgesetzt werden. Der Connector SHALL das neue Web-Dashboard nutzen (`/_app/remote/<hash>/<funktion>?payload=…`, devalue-kodierte Antworten). Die bisherige API (`api.arccosgolf.com`) läuft nicht mehr (Nutzerangabe 2026-11-14) und SHALL NOT unterstützt werden; auch Altdaten aus `arccos-fetch` (61 Runden, nur Stats-Ebene) können nur als Datei-Import in die Staging-Struktur geladen werden. Eine Hash-/Strukturänderung SHALL als `drift_detected` erscheinen. Schlagdaten SHALL, sofern geliefert, `startTerrain`/`endTerrain`, Distanz, GPS, Strafschläge und die quelleneigenen SG je Schlag in raw/staging erhalten.

Für den Web-Dashboard-Weg SHALL die Anmeldung per E-Mail-Magic-Link unterstützt werden: Shotlake löst den Link-Versand aus (`POST /api/login` mit `action=magic-link`) und nimmt den Link bzw. dessen Token vom Nutzer entgegen (UI/CLI/MCP-fähig); die resultierende Session SHALL als Secret gespeichert werden. Zugriff auf das Postfach SHALL NOT ohne ausdrückliche Konfiguration erfolgen. Bei abgelaufener Session SHALL der Status `auth_required` gesetzt und kein Passwort erfragt oder gespeichert werden.

Das Einlösen SHALL nach der bekannten Kette erfolgen (Mail-Link → `authentication.arccosgolf.com/v1/redeemMagicLink?token=…` → Weiterleitung mit `accessKey`, `secret`, `userId`). Der Connector SHALL Weiterleitungen beim Einlösen nicht blind verfolgen, sondern die Credentials aus der `Location` lesen, als Secret speichern und aus Logs/Fehlermeldungen maskieren (sie stehen in URLs). Ob und wie lange diese Credentials eine neue Session ermöglichen, ist in Phase 0 zu belegen; bis dahin SHALL der Connector ohne diese Annahme funktionieren (manuelles Einlösen bei `auth_required`).

#### Scenario: Credentials in URL
- WHEN die Redeem-Antwort die Credentials als Query-Parameter liefert
- THEN erscheinen sie weder in Logs noch im Import-Report noch in gespeicherten Raw-Metadaten

#### Scenario: Session abgelaufen
- WHEN der Sync mit 401/Redirect zur Login-Seite endet
- THEN steht die Quelle auf `auth_required`, vorhandene Daten bleiben nutzbar und die UI bietet „Magic-Link anfordern"

#### Scenario: Magic-Link einlösen
- WHEN der Nutzer den Link aus der E-Mail in Shotlake einfügt
- THEN wird die Session gespeichert, ohne dass Link oder Token in Logs oder Raw-Daten erscheinen

#### Scenario: Build-Wechsel der Web-App
- WHEN Arccos eine neue Web-App-Version ausliefert und ein Funktions-Hash nicht mehr aufgelöst werden kann
- THEN endet der Sync mit `drift_detected`, bestehende Daten bleiben unverändert und der Fehler nennt die betroffene Funktion

#### Scenario: Lie je Schlag
- WHEN ein Schlag `startTerrain=rough` und `endTerrain=green` liefert
- THEN steht Start- und End-Lie im Staging, ohne sie zu interpretieren oder zu ergänzen

Delta SHALL nicht nur über „erste bekannte Runde" erfolgen, sondern geänderte Runden anhand `lastModifiedTime`/`roundVersion` erkennen und neu laden. Runden mit `isDeleted=T` oder `shouldIgnore=T` SHALL importiert, aber aus Statistiken ausgeschlossen werden. Runden mit leerem Stats-Block SHALL als unvollständig markiert und beim nächsten Sync erneut versucht werden.

#### Scenario: Kein Exportweg
- WHEN Phase 0 keinen stabilen Weg ergibt
- THEN wird der Connector als `deferred` geführt und manueller Import (Scorecard-Ebene) genutzt

#### Scenario: Nachträglich bearbeitete Runde
- WHEN `roundVersion` einer bekannten Runde gestiegen ist
- THEN wird sie neu geladen und im Import-Report als „geändert" gemeldet

#### Scenario: Teilrunde
- WHEN eine Runde nur 11 Löcher enthält
- THEN wird sie mit `holes_played=11` gespeichert und nicht als vollständige 18-Loch-Runde gewertet

### Requirement: Connector FlightScope Mevo+
Der Mevo+-Connector SHALL CSV-Exporte aus myflightscope.com (Modus file) importieren, mit Messmethode `radar`, Quelle Range, Conditions aus `Mode` (Indoor/Outdoor). Eine Datei entspricht einer Session; Session-ID und Startzeit SHALL aus dem Dateinamen abgeleitet werden (Format z. B. `18-jul-2026_12_09_pm.csv`). Der Parser SHALL: die Summenzeilen `Avg`/`Dev` je Schlägerblock verwerfen, Richtungs-Suffixe (`R`/`L`) in vorzeichenbehaftete Werte wandeln, Fehlwerte (`-`, `- *`, `N/A`) als NULL lesen, imperiale Einheiten (yd, mph, ft, °F, psi, Zoll) nach SI umrechnen und beide bekannten Spaltenvarianten (2025: weniger Spalten; 2026: zusätzlich u. a. Club Path, Dynamic Loft, Low Point) akzeptieren, wobei fehlende Spalten NULL ergeben. Natural Key eines Schlags SHALL `(session, club, shot)` sein.

#### Scenario: Doppelter Datei-Import
- WHEN dieselbe CSV zweimal importiert wird
- THEN erkennt der Content-Hash sie und es entstehen keine Duplikate

#### Scenario: Summenzeilen
- WHEN ein Schlägerblock mit `Avg` und `Dev` endet
- THEN zählen diese Zeilen nicht als Schläge

#### Scenario: Ältere Exportvariante
- WHEN eine 2025er-Datei ohne Club-Path-Spalte importiert wird
- THEN ist `club_path` NULL und es wird kein Drift-Fehler gemeldet, weil die Variante bekannt ist

### Requirement: Connector BeBrassie
Der BeBrassie-Connector SHALL die bekannte interne Web-API nutzen (Login mit Session-Cookie, paginierte Rundenliste, Detailabruf je Runde) und in Phase 0 gegen Live verifiziert werden; Platzgeometrie und personenbezogene Felder SHALL vor der Speicherung entfernt werden. `scoreOnly`-Runden (ohne Schläge) SHALL importiert und als „nur Score" markiert werden. Bis zur Verifikation SHALL ein manueller Import (CSV-Template oder Formular) bereitstehen. Das ToS-Risiko SHALL dokumentiert sein.

#### Scenario: Manueller Import
- WHEN der Nutzer eine Runde per Template importiert
- THEN wird sie mit Quelle `bebrassie`, Modus `file`/`manual` und Provenienz gespeichert

### Requirement: Connector Trackman
Da kein Performance-Studio-Zugang besteht, SHALL der Trackman-Connector als **Primärweg die inoffizielle Web-API (GraphQL) mit dem eigenen Konto-Token** nutzen (Modus api); ein CSV-Import aus Performance Studio bleibt optional und ist erst relevant, wenn ein Export vorliegt. Das ToS-Risiko SHALL in `docs/sources/trackman.md` dokumentiert und der Connector per Config abschaltbar sein. Der Token SHALL aus einem Browser-Login stammen (laut Referenz `golf-coach` ca. 7 Tage gültig) und bei Ablauf `auth_required` auslösen. Aktivitäten (`COURSE_PLAY`, `SIMULATOR`, `VIRTUAL_RANGE`, `SHOT_ANALYSIS`, `SESSION` u. a.) SHALL über ihre IDs inkrementell geholt werden; nicht-golfanalytische Arten (z. B. `VIDEO`, `DYNAMIC_REPORT`) SHALL ignoriert, aber im Import-Report gezählt werden. Die API-Antworten SHALL gegen ein Schema geprüft werden, da die GraphQL-Abfragen bei Schemaänderungen brechen (siehe Szenario).

Der Trackman-Zugriff SHALL über `POST https://api.trackmangolf.com/graphql` mit `Authorization: Bearer <Token>` erfolgen, Details per `node(id)`-Abfrage je Aktivitätsart. Felder, die je Aktivitätsart unterschiedlich nullable sind (z. B. `time` je Range-Schlag), SHALL mit Alias abgefragt werden. Platz-Sim-Schläge SHALL `launchLie` und `finalLie` im Staging erhalten. Die Abfragen SHALL versioniert im Repo liegen (`connectors/trackman_queries.py`) und per Contract-Test gegen Fixtures geprüft werden; sie sind zusätzlich gegen die offene Schema-Introspection validiert. Das Token SHALL aus `SHOTLAKE_TRACKMAN_TOKEN` oder der Token-Datei des `golf-coach`-Plugins kommen; abgelaufen = `AuthError`. Sim-Runden (Scorecards) SHALL als `simulated` importiert werden, nie in Score Differential eingehen und nicht mit realen Runden dedupliziert werden.

#### Scenario: Nullable-Zeit je Range-Schlag
- WHEN ein Range-Schlag `time` ohne Wert liefert
- THEN wird der Schlag mit `shot_time=NULL` importiert und die Session-Zeit stammt aus der Aktivität

#### Scenario: Course-Sim über API
- WHEN eine Course-Simulation (z. B. `COURSE_PLAY`) synchronisiert wird
- THEN werden Runde, Löcher (Par, Index, Distanz, Brutto/Netto, Putts, GIR) und Aggregate angelegt, mit Conditions `simulated=true`, `indoor=true`; fehlende Felder bleiben NULL

#### Scenario: Unvollständige Runde
- WHEN eine Runde `isCompleted=false` ist (z. B. 5 von 9 Löchern)
- THEN wird sie als unvollständig gespeichert, nicht für Handicap/Statistik gezählt und beim nächsten Sync aktualisiert

#### Scenario: GraphQL-Schemaänderung
- WHEN die API mit einem Typkonflikt-Fehler (z. B. Feld `time` `DateTime` vs. `DateTime!`) antwortet
- THEN endet der Lauf mit `drift_detected`, Raw und canonical bleiben unverändert und die betroffene Abfrage wird genannt

### Requirement: Fixtures je Quelle
Zu jedem Connector SHALL anonymisierte Beispiel-Fixtures im Repo liegen (keine Secrets, keine Klarnamen/GPS-Heimadresse).

#### Scenario: Neue Quelle ohne Fixture
- WHEN ein Connector ohne Fixtures eingereicht wird
- THEN schlägt die CI-Prüfung fehl

### Requirement: Dokumentation je Connector
Je Connector SHALL dokumentiert sein: Modus, Auth, Cursor, Retry, Felder-Mapping, ToS-/Stabilitätsrisiko, Fallback.

#### Scenario: Risiko sichtbar
- WHEN `list_sources` aufgerufen wird
- THEN enthält jede Quelle ein Feld `risk` (official | unofficial_tos_risk | file_only)
