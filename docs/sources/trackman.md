# Quelle: Trackman Golf

Stand: 2026-11-19. Connector: `src/shotlake/connectors/trackman.py` (Parser, Sync),
`trackman_live.py` (GraphQL-Client) und `trackman_queries.py` (Abfragen). Quelle-ID: `trackman`,
Modus `api`, Risiko `unofficial_tos_risk` (inoffizielle Web-API, Token aus einer Browser-Anmeldung).

## Was die Quelle liefert

- **Sessions** (Range, Virtual Range, Shot Analysis, Map My Bag, Simulator) mit Radarschlägen:
  Ballgeschwindigkeit, Carry, Gesamtdistanz, Seitenabweichung, Abflugwinkel, Spin, Schlägerdaten
  (Geschwindigkeit, Bahn, Winkel, Smash Factor) und mehr.
- **Scorecards** aus dem Simulator (Platz-Sim) mit Löchern und Schlägen. Sie werden als simulierte
  Runden (`simulated = true`) gespeichert, nicht in Auswertungen mit echten Runden gemischt und
  nicht für Score Differential oder Handicap verwendet.

## Zugang

Die Schnittstelle ist GraphQL: `https://api.trackmangolf.com/graphql`. Der Web-Client von Trackman
ist vertraulich (kein eigener OAuth-Austausch möglich). Shotlake nutzt daher ein Bearer-Token aus
einer Browser-Anmeldung auf portal.trackmangolf.com (Header `Authorization` einer Anfrage an die
GraphQL-Adresse, ohne „Bearer"). Das Token gilt etwa 7 Tage.

Reihenfolge der Quellen für das Token:
1. `SHOTLAKE_TRACKMAN_TOKEN` in der Umgebung, im verschlüsselten Speicher der Oberfläche oder sops,
2. die Token-Datei des `golf-coach`-Plugins (`~/.golf-coach/token.json`, Feld `access_token`).

Ein abgelaufenes Token (Prüfung des JWT-Felds `exp` mit 2 Minuten Spielraum) ergibt einen
Anmeldefehler. Das Token steht nie in Meldungen oder Reprs.

## Abfragen

Eigene, versionierte Abfragen (`QUERY_VERSION = 2026-11-14`), gegen die offene Schema-Introspection
geprüft:

- `LIST_ACTIVITIES`: Aktivitäten der Arten `RANGE_PRACTICE`, `RANGE_FIND_MY_DISTANCE`,
  `SHOT_ANALYSIS`, `VIRTUAL_RANGE`, `SESSION`, `MAP_MY_BAG`, `SIMULATOR` in Seiten zu 50, mit
  `includeHidden`.
- `SESSION_DETAIL`: Schläge einer Aktivität über Fragmente je Aktivitätsart.
- `SCORECARDS`: Platzrunden mit Löchern, Schlägen, Tees und Geokoordinaten, in Seiten zu 10 (die
  Antworten sind schwer; bei 50 Runden pro Seite kam es zu Timeouts, der Client wartet bis 90 s).
- `PROFILE_CHECK`: kleine Abfrage für „Prüfen" in der Oberfläche.

Die Zeitfelder heißen je Schlagart unterschiedlich nullable (`RangeStroke.time` ist `DateTime`,
`Stroke.time` ist `DateTime!`). Deshalb tragen sie die Alias `rangeTime` und `strokeTime`; sonst
lehnt der Server die Abfrage wegen widersprüchlicher Typen ab.

## Fehler und Wiederholungen

- **Serverfehler** („Unexpected Execution Error", `INTERNAL_SERVER_ERROR`, HTTP 5xx) sind keine
  Schemaänderung. Der Client wiederholt die Abfrage bis zu dreimal mit Wartezeit; bleibt es dabei,
  scheitert nur dieser Teil, nicht der ganze Sync.
- Eine **Aktivität**, deren Details nicht kommen, wird übersprungen und gemeldet; der Cursor für
  Sessions bleibt vor ihr stehen, sie kommt beim nächsten Lauf nach.
- Scheitert eine **Seite Scorecards**, wird sie in Einzelabfragen zerlegt. Eine fehlerhafte Scorecard
  wird übersprungen (Meldung mit Position), die übrigen kommen an. Der Runden-Cursor rückt dann nicht
  vor, damit nichts verloren geht.
- Meldungen nennen Abfrage, Pfad und Code des GraphQL-Fehlers (nie das Token). Nur echte
  Abfragefehler (zum Beispiel unbekanntes Feld) gelten als Schemaänderung (`drift_detected`).

## Rohdokumente

Zwei Arten, je eines pro Aktivität bzw. Scorecard:

- `{"_kind": "session", "id", "time", "kind", "strokes": [...]}` → `ParsedSession`
- `{"_kind": "round", "id", "startedAt", "course", "tees", "holes": [...], ...}` → `ParsedRound`

Die Kennung ist eine Base64-Zeichenkette `Typ\n<Kennung>`; bei Scorecards steht eine zweite Kennung
(die Spieler-ID) nach einem Doppelpunkt. `lastUpdatedAt` ändert sich ohne Inhaltsänderung und zählt
deshalb nicht für den Raw-Hash.

## Zuordnung auf das kanonische Modell

**Einheiten** (laut Trackman-Doku, live bestätigt): Geschwindigkeit m/s, Distanz m, Spin rpm,
Winkel Grad. Die Seitenabweichung wird rechts positiv übernommen (Trackman-Konvention, an echten
Daten noch nicht gegengeprüft).

**Sessions:**
- Methode `radar`, bei Art `SIMULATOR` die Methode `sim` und `indoor = true`. Sim und Radar werden
  in Auswertungen getrennt, damit Distanzen nicht mischen.
- Spin: `spinRate`, sonst `ballSpin` (Range-Aktivitäten).
- Fehlt die Zeit eines Schlags, bleibt sie `NULL` (kein erfundener Wert).
- Teilschläge zählen nicht mit: Ballgeschwindigkeit unter 70 % des Medians je Schläger und Methode.

**Schläger:** Enum-Namen werden deterministisch zugeordnet (`IRON7` → `iron_7`, `WOOD3` →
`wood_3`, `HYBRID6`, `PITCHING_WEDGE` → `wedge_pitching`, `DRIVER`, `PUTTER`). Namen wie `52Wedge`
oder `WEDGE52` bleiben ungeraten und stehen in `config/club_map.toml` (Abschnitt `[trackman]`:
52 → gap, 56 → sand, 60 → lob). Ein Schlag ohne Namen lässt sich über
`config/club_overrides.toml` nach Zeitfenster zuordnen (füllt nur Schläge ohne Typ).

**Scorecards:**
- Durch die Sim-Kennzeichnung (`simulated`) nie in gemeinsamen Statistiken, Dedup oder Handicap;
  Auswertung nur gezielt (`--source trackman` oder Rundenkennung).
- `isCompleted = false` ergibt `in_progress`; solche Runden bleiben zurück, bis sie fertig sind.
- Tees: `gender` kommt als `Female`/`Male` (Groß-/Kleinschreibung variiert), wird zu `WOMEN`/`MEN`.
  Rating und Slope stehen am Tee; die Runde selbst trägt keines (nicht handicap-relevant).
- Putts sind **keine** Schläge in den Daten. Passt Schlagzahl + Putts zum Lochscore, gibt es keine
  Strafschläge (0), sonst bleibt die Differenz unbekannt (`NULL`, nicht geraten). Von 142 Löchern
  waren bei der ersten Auswertung 81 vollständig.
- Fahnenposition und Schlagpositionen kommen aus `worldLocation` (Breite/Länge). Bei Platz-Sims
  fehlen sie teilweise; dann gibt es die Warnung „Sim-Runde ohne Geokoordinaten" und keine
  Strokes Gained.

**Lagen:** `tee`, `fairway`, `rough`, `bunker`/`sand` → `sand`, `green`, `hole`, `water` → `other`,
`deeprough` und `semirough` → `rough` (Näherung), `holecup` → `hole`. Vorgrün (`fringe`), Fels, Mulch
und Kiefernnadeln werden `other`, weil die Baseline keine Tabellen dafür hat. `None` bleibt
`NULL`. Unbekannte Lagen und Schlägernamen werden gemeldet, nie geraten.

## Cursor und Idempotenz

Der Cursor ist JSON `{"range", "round"}`. Aktivitäten werden mit einem Tag Überlappung ab dem
`range`-Cursor geholt (Duplikate fängt der Raw-Hash ab). Eine offene (nicht abgeschlossene) Runde hält
den `round`-Cursor zurück, damit sie später vollständig nachkommt. Aktivitäten ohne Schlagfragment
werden übersprungen und gemeldet („ohne Schlagdaten").

## Bekannte Grenzen

- Token läuft nach etwa 7 Tagen ab und muss erneuert werden; ein automatischer Refresh ist nicht
  möglich (vertraulicher Client).
- Schläger mit Zahlen im Namen (`52Wedge`) brauchen die manuelle Zuordnung.
- Seitenvorzeichen noch nicht gegen Messungen geprüft.
- Aktivitätsarten `RANGE_PRACTICE` und `RANGE_FIND_MY_DISTANCE` kamen in den bisherigen Daten nicht
  vor und sind nur mit synthetischen Dokumenten getestet.
- Sim-Runden sind nicht mit echten Runden vergleichbar (Strokes Gained roh etwa −0,73 pro Loch
  gegenüber −1,60 bei echten Runden, mit anderer Datenlage).

## Fixtures und Tests

`tests/fixtures/trackman/` enthält sieben anonymisierte Rohdokumente:

| Datei | Fall |
|---|---|
| `session_session.json` | Aktivität `SESSION`, 3 Radarschläge (7er-Eisen) |
| `session_shot_analysis.json` | `SHOT_ANALYSIS`, 2 Schläge |
| `session_map_my_bag.json` | `MAP_MY_BAG`, 4 Schläge |
| `session_virtual_range.json` | `VIRTUAL_RANGE`, 2 Schläge |
| `session_simulator.json` | `SIMULATOR`, 6 Schläge, Methode `sim`, ein ungeratener Wedge-Name |
| `scorecard_incomplete.json` | offene Runde (9 Löcher, 2 Schläge), `in_progress` |
| `scorecard_complete.json` | abgeschlossene Runde, 18 Löcher, 57 Schläge, Tee mit Rating 71,2 / Slope 128 |

Anonymisiert mit `scripts/anonymize_fixture.py` (Kennungen inklusive der Spieler-ID in den
Base64-Kennungen, Zeiten, Länge der Koordinaten). Tests: `tests/test_api_fixtures.py` (Parser je
Aktivitätsart, Einheiten, Sync mit gespielten Fixtures und Idempotenz) und `tests/test_trackman.py`
(Regeln, Client, Cursor, Token).
