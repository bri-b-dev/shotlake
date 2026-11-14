# dedup

## ADDED Requirements

### Requirement: Idempotente Imports
Wiederholte Imports derselben Daten SHALL dasselbe Ergebnis liefern. Identität SHALL über stabile Quell-IDs bzw. Content-Hash bestimmt werden; Änderungen an bekannten Records SHALL als `geändert` (neue Version, alte erhalten) gemeldet werden.

#### Scenario: Re-Import
- WHEN dieselbe Datei erneut importiert wird
- THEN bleibt die Zeilenanzahl in canonical unverändert

#### Scenario: Quelle korrigiert Wert
- WHEN Garmin nachträglich einen Score ändert
- THEN wird der Record aktualisiert, die Vorversion bleibt in raw und der Report zeigt „geändert"

### Requirement: Matching derselben Runde über Quellen
Das System SHALL Runden aus mehreren Quellen (Garmin, Arccos, BeBrassie, Trackman) derselben Runde zuordnen. Zwei Runden verschiedener Quellen sind Kandidaten, wenn sie denselben **Club** haben (Name oder konfigurierter Alias, oder Koordinaten innerhalb eines konfigurierbaren Radius), ihre Startzeiten höchstens 90 Minuten (konfigurierbar) auseinanderliegen und die Lochzahl um höchstens 1 abweicht. Das Ergebnis SHALL mit Konfidenz (hoch, mittel, niedrig) und Regelbezug gespeichert werden. Zwei Runden derselben Quelle SHALL NOT verknüpft werden; laufende Runden SHALL ausgeschlossen sein.

#### Scenario: Eindeutiger Treffer
- WHEN zwei Runden gleichen Club, Start innerhalb der Toleranz, gleiche Lochzahl und gleichen Score haben und beide die einzigen Kandidaten sind
- THEN werden sie automatisch verknüpft (Konfidenz hoch)

#### Scenario: Mehrdeutig oder abweichend
- WHEN mehrere Kandidaten passen, der Score abweicht oder die Lochzahl abweicht
- THEN bleibt der Match `vorgeschlagen` (Konfidenz mittel/niedrig) und wird zur Entscheidung angezeigt; Runden mit mehr als 10 Schlägen Unterschied bei gleicher Lochzahl sind keine Kandidaten

#### Scenario: Gleiche Runde, drei Quellen
- WHEN Garmin, Arccos und BeBrassie dieselbe Runde melden
- THEN bilden sie eine Gruppe verknüpfter Runden mit höchstens einer Runde je Quelle

### Requirement: Plätze und Aliasse
Das System SHALL Plätze über eine Konfiguration demselben Club zuordnen können (Aliasliste, z. B. alter und neuer Clubname). Ein Club SHALL mehrere Plätze haben können (z. B. 9- und 18-Loch-Platz in einer Quelle); für den Abgleich SHALL die gespielte Lochzahl der Runde entscheiden, nicht der Platz.

#### Scenario: Umbenannter Club
- WHEN eine Quelle den Club „Golfclub Herrnhof" und eine andere „Golfclub Neumarkt" nennt und beide Namen als Alias desselben Clubs konfiguriert sind
- THEN gelten sie als derselbe Club

#### Scenario: 18-Loch-Platz mit 9 gespielten Löchern
- WHEN eine Runde auf dem 18-Loch-Platz nur 9 Löcher umfasst und eine andere Quelle dieselbe Runde auf dem 9-Loch-Platz führt
- THEN passen sie zusammen, weil Club und Lochzahl übereinstimmen

### Requirement: Matching von Range-Sessions
Range-Sessions verschiedener Geräte (z. B. R10 und Mevo+ am selben Tag) SHALL NICHT automatisch zusammengeführt, sondern als parallele Messungen derselben Session verknüpfbar sein, damit Vergleich je Messmethode möglich ist.

#### Scenario: Gleiches Training, zwei Geräte
- WHEN R10 und Mevo+ am selben Tag überlappende Zeiten haben
- THEN schlägt das System eine Verknüpfung vor, Shots werden nicht zusammengelegt

### Requirement: Führende Quelle je Feld
Für verknüpfte Runden SHALL pro Feldgruppe (Score, Schläge/Lagen/Putts/SG, Rating/Slope) eine Prioritätsreihenfolge konfigurierbar sein; die erste Quelle **mit Daten** in dieser Gruppe führt. Auswertungen ohne Quellenfilter SHALL jede verknüpfte Runde einmal zählen (führende Quelle), nicht-führende Werte SHALL erhalten bleiben und Konflikte (abweichende Scores) in der Runde angezeigt werden. Fehlt Rating/Slope in der führenden Quelle, SHALL es aus der verknüpften Runde mit der nächsten Priorität übernommen und als übernommen gekennzeichnet werden.

#### Scenario: Abweichender Score
- WHEN Quellen unterschiedliche Scores melden
- THEN gilt der Wert der führenden Quelle, der Konflikt wird in der Runde angezeigt

#### Scenario: Rating nur in einer Quelle
- WHEN BeBrassie keinen Wert für das Rating liefert, die verknüpfte Garmin-Runde aber schon
- THEN erhält die Runde ein Score Differential mit dem Rating von Garmin

### Requirement: Manuelle Korrektur
Der Nutzer SHALL Vorschläge bestätigen, Verknüpfungen trennen, beliebige Runden zusammenführen und alle offenen Vorschläge auf einmal bestätigen können. Entscheidungen SHALL persistiert werden und spätere Automatik übersteuern: Eine abgelehnte Verknüpfung wird nie wieder automatisch gebildet, eine bestätigte bleibt bestehen. Ein Zusammenführen, das zwei Runden derselben Quelle in eine Gruppe legte, SHALL abgelehnt werden.

#### Scenario: Zusammenführung rückgängig
- WHEN der Nutzer einen Match trennt
- THEN wird er nicht automatisch erneut gebildet

#### Scenario: Zusammenführen entgegen den Regeln
- WHEN der Nutzer zwei Runden zusammenführt, deren Startzeiten weit auseinanderliegen
- THEN werden sie verknüpft und bleiben es bei jedem weiteren Abgleich
