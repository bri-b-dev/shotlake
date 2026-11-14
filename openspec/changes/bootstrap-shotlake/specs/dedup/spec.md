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
Das System SHALL Runden aus mehreren Quellen (z. B. Garmin + Arccos + BeBrassie) einer kanonischen Runde zuordnen, nach Regeln: gleicher Course (via Alias/Geo), Startzeit innerhalb Toleranz (Default ±90 min), Lochzahl, Gesamtscore-Übereinstimmung oder -Nähe. Ergebnis SHALL mit Konfidenz und Regelbezug gespeichert werden.

#### Scenario: Eindeutiger Treffer
- WHEN zwei Runden gleichen Platz, Start ±20 min und gleichen Score haben
- THEN werden sie automatisch verknüpft (Konfidenz hoch)

#### Scenario: Mehrdeutig
- WHEN mehrere Kandidaten passen oder Scores abweichen
- THEN bleibt der Match `vorgeschlagen` und wird zur manuellen Entscheidung angezeigt

### Requirement: Matching von Range-Sessions
Range-Sessions verschiedener Geräte (z. B. R10 und Mevo+ am selben Tag) SHALL NICHT automatisch zusammengeführt, sondern als parallele Messungen derselben Session verknüpfbar sein, damit Vergleich je Messmethode möglich ist.

#### Scenario: Gleiches Training, zwei Geräte
- WHEN R10 und Mevo+ am selben Tag überlappende Zeiten haben
- THEN schlägt das System eine Verknüpfung vor, Shots werden nicht zusammengelegt

### Requirement: Führende Quelle je Feld
Für verknüpfte Runden SHALL pro Feldgruppe eine Prioritätsreihenfolge konfigurierbar sein (z. B. Score: manuell > BeBrassie > Garmin; Schläge/Lie: Arccos > Garmin); Nicht-führende Werte SHALL erhalten bleiben.

#### Scenario: Abweichender Score
- WHEN Quellen unterschiedliche Scores melden
- THEN gilt der Wert der führenden Quelle, der Konflikt wird in der Runde angezeigt

### Requirement: Manuelle Korrektur
Der Nutzer SHALL Matches bestätigen, trennen, zusammenführen und Konflikte auflösen können; Entscheidungen SHALL persistiert werden und spätere Auto-Matches übersteuern.

#### Scenario: Zusammenführung rückgängig
- WHEN der Nutzer einen Match trennt
- THEN wird er nicht automatisch erneut gebildet
