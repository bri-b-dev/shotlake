# Quelle: BeBrassie

Stand: 2026-11-19. Connector: `src/shotlake/connectors/bebrassie.py` (Parser, Sync) und
`bebrassie_live.py` (HTTP-Client). Quelle-ID: `bebrassie`, Modus `api`, Risiko
`unofficial_tos_risk` (inoffizielle Web-API der App, kann sich ohne Ankündigung ändern).

## Was die Quelle liefert

Platzrunden mit Lochscore, Par und Lochindex je Geschlecht, Rating/Slope des Tees und einzelnen
Schlägen mit GPS-Start und -Ziel, Schläger, Lage und Schlagart. Putts sind eigene Schläge.
BeBrassie liefert keine Radardaten (kein Carry, keine Ballgeschwindigkeit).

## Zugang

1. `POST /login` mit E-Mail und Passwort; die Sitzung hält das Cookie. Ein Bearer-Token wird nur
   gesetzt, wenn die Antwort eines der Felder `accessToken`, `access_token`, `access-token`,
   `token` enthält (der tatsächliche Feldname ist unbestätigt; das Cookie genügt offenbar).
2. `GET PlayedRound?deepPopulate=course&limit&skip&sort&query` liefert die Rundenliste in Seiten zu
   50. Die Gesamtzahl steht im Header `x-total-count`. Der Filter entspricht dem der App:
   `signedRound` oder `scoreOnly` (keine laufenden Runden).
3. `GET show-round/<id>` liefert `{success, data}` mit Löchern und Schlägen.

Zugangsdaten kommen aus der Umgebung, dem verschlüsselten Speicher der Oberfläche oder sops
(`SHOTLAKE_BEBRASSIE_EMAIL`, `SHOTLAKE_BEBRASSIE_PASSWORD`). Sie stehen nie in Meldungen.

## Rohdokument

Je Runde ein Dokument: `{**Listeneintrag, **show-round}`, wobei das Detail gewinnt. Vor dem Speichern
entfernt `slim()` die Platzgeometrie (rund 1,3 MB: `course.courseData`, `courseTee.locations`,
`courseTee.teeboxShapes`, `courseTees`), `ogTags` und `user` (Klarname). Zusätzlich fallen
`userAgent`, `note`, `images`, `aiReportId` weg. Rollende Durchschnitte (`hciAvg10`, `firAvg10`,
`girAvg10`, `sgAvg10`, `wttAvg10`) ändern sich bei jedem Lauf und zählen nicht für den Raw-Hash.

Wichtige Felder:

| Feld | Bedeutung |
|---|---|
| `_id`, `startedAt`, `finishedAt` | Rundenkennung und Zeiten (UTC, ISO 8601) |
| `roundType` | `social`, `tournament`, `practice` |
| `course.name`, `course.golfClub.name/address` | Platz; Adresse mit GeoJSON-Punkt |
| `courseTee` | `name`, `ratingWomen/Men`, `slopeWomen/Men`, `totalPar` |
| `courseHoles[]` | `holeNumber`, `parWomen/Men`, `handicapWomen/Man`, `location` (Fahne) |
| `playedHoles[]` | `holeNumber`, `numShots`, `courseHolePar`, `isPickup`, `holeIndex` |
| `shots[]` | `number` (ab 0), `courseHole`, `club`, `shotType`, `shotFrom`, `shotTo`, `location`, `destination`, `calculatedDistance` |
| `pinPositions` | Fahnenposition je `holeIndex` |
| `proPlayerType`, `strokesGained` | Wertung (zum Beispiel `WOMEN18`) |
| `scoreOnly`, `totalHoles`, `totalShots`, `totalToPar` | Kopfwerte der reinen Score-Runden |

## Zuordnung auf das kanonische Modell

- **Koordinaten:** GeoJSON `[Länge, Breite]`; werden zu (Breite, Länge). Leere Koordinaten ergeben
  `NULL`. `calculatedDistance` ist in Metern (GPS Start bis Ende inklusive Rollen, kein Carry).
- **Schläge:** Putts sind Schläge (`shotType = putt`, Start auf dem Grün); die Zahl der Schläge je
  Loch entspricht immer `numShots`. Die Reihenfolge ergibt sich aus `number`.
- **Strafschläge:** stehen als eigener Schlag (`penalty`). Sie werden nicht als Schlag gespeichert,
  sondern als Strafschlag des Lochs gezählt; der Schlag davor endet am Droppunkt.
- **Putts je Loch:** Anzahl der Schläge mit Start auf dem Grün, nur wenn die Schlagliste vollständig
  ist (Anzahl gleich `numShots`), sonst `NULL`. Gleiches gilt für Strafschläge.
- **Aufgenommene Löcher** (`isPickup`) haben keinen Lochscore und erzeugen eine Warnung.
- **Fairway:** Treffer, wenn der Abschlag auf dem Fairway endet, sonst Fehlschlag. Die Seite ist
  nicht belegt, deshalb gibt es kein links/rechts. Nur bei Par 4 und höher.
- **Wertung:** Damen oder Herren folgt `proPlayerType` bzw. dem Schlüssel in `strokesGained`. Ohne
  Angabe gibt es kein Rating an der Runde (nicht geraten), aber eine Warnung. Lochindex kommt aus
  `handicapWomen` bzw. `handicapMan` der gleichen Wertung.
- **Rating/Slope:** 0 bedeutet „nicht gepflegt" und ist kein Wert. Rating und Slope gelten nur
  gemeinsam.
- **Schläger:** `clubShortcut` ist die Position im Bag (`D1`, `I7`, `56°`), `clubName` nur das
  Modell. Die Zuordnung zu kanonischen Typen steht in `config/club_map.toml` (Abschnitt
  `[bebrassie]`).
- **Lagen:** `tee`/`teebox` → `tee`, `fairway`, `rough`, `bunker`/`sand` → `sand`, `green`, `hole`,
  `water` → `other`. `roughout` ist laut Nutzerin „tiefes Rough" und wird als `rough` gezählt
  (Näherung, tendenziell zu milde, die Baseline kennt keine eigene Lage dafür).
- **Schlagarten:** `teeshot` → TEE, `shottogreen` → APPROACH, `chip`, `pitch`, `putt`,
  `transport`, `bunker`, `recovery`, `penalty`.

## Sonderfälle

- **Reine Score-Runden** (`scoreOnly`): `show-round` antwortet mit HTTP 400, deshalb wird die Anfrage
  gar nicht erst gestellt. Die Runde wird mit den Kopfwerten (Löcher, Schläge, zu Par) importiert,
  ohne Löcher, Schläge, Tee und Rating, und bekommt die Warnung „nur Listeneintrag ohne Lochdaten".
  Sie hält den Cursor nicht zurück.
- **Teilrunden** (zum Beispiel 5 oder 11 Löcher) kommen vor. Ohne volle 9 oder 18 Löcher gibt es kein
  Score Differential.
- **Listenreihenfolge** ist nicht verifiziert. Der Sync bricht deshalb nie anhand der Reihenfolge ab.

## Cursor und Idempotenz

Der Cursor ist der neueste `startedAt` unter den Runden, die vollständig geholt wurden (mit Detail
oder reine Score-Runde). Fehlt bei einer normalen Runde das Detail (zum Beispiel wegen eines
Fehlers), bleibt der Cursor stehen und die Runde wird beim nächsten Lauf erneut versucht; der Sync
meldet „Runden ohne Detaildaten". Ein zweiter Lauf mit unverändertem Inhalt ergibt keine neuen
Runden (Raw-Hash über den stabilen Teil des Dokuments).

## Bekannte Grenzen

- Feldname des Tokens in der Login-Antwort unbestätigt.
- Laufzeit der Sitzung unbekannt; bei Ablauf meldet der Sync einen Anmeldefehler.
- Seite des Fehlschlags (links/rechts) fehlt in den Daten.
- Keine Wetter-, Schlägerdistanz- oder Radardaten werden übernommen (auch wenn die Quelle
  `weather` und `clubsDistances` liefert).

## Fixtures und Tests

`tests/fixtures/bebrassie/` enthält drei anonymisierte Rohdokumente:

| Datei | Fall |
|---|---|
| `score_only_round.json` | reine Score-Runde, nur Kopfwerte (9 Löcher, 56 Schläge) |
| `partial_five_holes.json` | Teilrunde mit 5 Löchern, 35 Schlägen, Rating 70,3 / Slope 136 |
| `nine_holes_with_penalty.json` | 9 Löcher, 56 Schläge, ein Strafschlag auf Loch 8 |

Anonymisiert mit `scripts/anonymize_fixture.py` (Kennungen, Zeiten, Länge der Koordinaten,
Adresse, Platz- und Ortsnamen). Tests: `tests/test_api_fixtures.py` (Parser, Contract-Check, Sync
und Idempotenz) und `tests/test_bebrassie.py` (Parser-Regeln mit synthetischen Fällen).
