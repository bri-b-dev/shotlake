# canonical-model

## ADDED Requirements

### Requirement: Datenschichten
Das System SHALL vier Schichten führen: raw (unverändert, versioniert) → staging (pro Quelle geparst) → canonical (einheitlich) → marts (Auswertungen). Staging, canonical und marts SHALL aus raw vollständig reproduzierbar sein.

#### Scenario: Rebuild
- WHEN `rebuild --from raw` läuft
- THEN entsteht dieselbe canonical-Datenbasis wie zuvor (bis auf bewusst geänderte Regeln)

### Requirement: Canonical-Entitäten
Canonical SHALL enthalten: Session/Round, Hole, Shot, Club, Course (+ Tee, Rating, Slope, Par, Länge), Source, Conditions (Wind, Temperatur, Höhe, indoor/outdoor, real/simuliert), Ball, Note/Annotation.

#### Scenario: Runde mit Löchern
- WHEN eine Platzrunde importiert wird
- THEN besteht sie aus Round → Holes → Shots mit Referenzen auf Course/Tee und Club

### Requirement: Shot-Felder
Ein Shot SHALL tragen: Quelle, Messmethode, Zeit, Carry, Total, Ballgeschwindigkeit, Schlägerdaten (Speed, Face/Path, Attack Angle, sofern vorhanden), Launch, Spin, Seitenabweichung, Start-/Endlie, Start-/Endposition, Distanz zum Loch (Start/Ende), Schläger.

#### Scenario: Fehlende Felder
- WHEN eine Quelle Spin nicht liefert
- THEN ist `spin_rpm` NULL und es wird nichts abgeleitet, geschätzt oder erfunden

### Requirement: Gemeinsames Lie-Vokabular
Start- und End-Lie eines Schlags SHALL auf ein gemeinsames Vokabular (`tee`, `fairway`, `rough`, `sand`, `green`, `other`) abgebildet werden; die Quell-Bezeichnung (z. B. `TeeBox`, `Bunker`) und die Herkunft des Lie (`automatic`/`manual`, z. B. aus `lieSource`) SHALL erhalten bleiben. Nicht abbildbare Werte SHALL als `other` mit Originalwert gespeichert und im Import-Report genannt werden.

#### Scenario: Garmin-Lie
- WHEN ein Garmin-Schlag `startLoc.lie=Bunker` und `lieSource=CARTOGRAPHY` hat
- THEN ist `start_lie=sand` und `start_lie_source=automatic`, der Originalwert bleibt im Staging

### Requirement: Einheiten und Zeit
Intern SHALL SI verwendet werden (m, m/s, °C, rpm, Grad); Anzeige m/yd SHALL umschaltbar sein; Zeitstempel SHALL in UTC gespeichert und als Europe/Berlin dargestellt werden.

#### Scenario: Yard-Quelle
- WHEN eine Quelle in yd/mph liefert
- THEN wird beim Parsen exakt in SI umgerechnet und die Originaleinheit im Staging vermerkt

#### Scenario: Zeitumstellung
- WHEN eine Session um 02:30 am Umstellungstag liegt
- THEN wird sie eindeutig über UTC zugeordnet

### Requirement: Provenienz
Jeder canonical-Datensatz SHALL auf seine Raw-Objekte (Quelle, Hash, Connector-Version, Importlauf) verweisen.

#### Scenario: Herkunft abfragen
- WHEN ein Shot angezeigt wird
- THEN lässt sich Quelle, Rohdatei und Importlauf nachvollziehen

### Requirement: Notes/Annotations
Notizen SHALL typisiert (equipment | swing | coach | other), datiert (Gültig-ab, optional -bis) und an Club/Session/Zeitraum koppelbar sein und in Trendcharts als Marker erscheinen.

#### Scenario: Equipment-Wechsel
- WHEN eine Notiz „Neuer Driver-Schaft" am 2026-05-01 hinzugefügt wird
- THEN erscheint sie als Marker in Trends des Drivers

### Requirement: Datenqualitätsregeln
Beim Übergang nach canonical SHALL Plausibilitätsprüfungen laufen (z. B. Carry > Total, negative Geschwindigkeit); Verstöße SHALL markiert (`quality_flag`), nicht stillschweigend korrigiert oder gelöscht werden.

#### Scenario: Ausreißer
- WHEN ein Schlag 400 m Carry hat
- THEN wird er mit `quality_flag=outlier` gespeichert und standardmäßig aus Statistiken ausgeschlossen (umschaltbar)
