# analytics-sg

## ADDED Requirements

### Requirement: SG-Formel
Strokes Gained je Schlag SHALL berechnet werden als `E(Start) − E(Ende) − 1`, wobei E die erwarteten Restschläge der Baseline für (Lie, Distanz) ist; Strafschläge SHALL zusätzlich einzeln eingerechnet werden; Einlochen hat E = 0.

#### Scenario: Golden Case
- WHEN E(Start)=4.00, E(Ende)=2.80
- THEN SG = 0.20

#### Scenario: Strafschlag
- WHEN ein Schlag ins Wasser geht und 1 Strafschlag folgt
- THEN wird der Strafschlag in E(Ende)/Schlagzahl berücksichtigt und dem verursachenden Schlag zugeordnet

### Requirement: Quellenberechnete vs. selbst berechnete SG
SG-Werte, die eine Quelle bereits liefert (z. B. Arccos `sga/drivingSga/approachSga/shortSga/puttingSga` gegen ein Ziel-Handicap, Garmin `strokesGainedRatings` gegen eine Vergleichsgruppe), SHALL als `source_provided` mit Quelle und deren Baseline-Parameter gespeichert und ausgewiesen werden; selbst berechnete SG als `computed`. Beide Arten SHALL NICHT ohne Kennzeichnung in einem Aggregat gemischt werden, da die Baselines nicht vergleichbar sind.

#### Scenario: Arccos-Runde ohne Lie
- WHEN für eine Arccos-Runde keine Lie-Daten je Schlag vorliegen, aber Arccos-SG vorhanden sind
- THEN zeigt das System die Arccos-SG als `source_provided (goalHcp=X)` und keine `computed`-SG

### Requirement: Konfigurierbare, dokumentierte Baseline
Die Baseline SHALL wählbar sein (Tour, Scratch, Handicap-Band X); Standard ist das **Handicap-Band** mit konfigurierbarem Ziel-Handicap und als versionierte Tabelle (Lie × Distanz → erwartete Schläge) mit Quellenangabe und Lizenzhinweis im Repo liegen. Jede SG-Ausgabe SHALL die verwendete Baseline-ID nennen.

#### Scenario: Baseline-Wechsel
- WHEN der Nutzer von Tour auf Handicap 10 wechselt
- THEN werden SG-Werte neu berechnet und die Baseline-ID in der Ausgabe angezeigt

### Requirement: Kategorien
SG SHALL in Off-the-Tee, Approach, Short Game, Putting zerlegt werden; Grenzen (Default: Tee = erster Schlag Par 4/5; Short Game = ≤ 50 m vom Grün, nicht auf Grün; Putting = auf Grün; Rest = Approach) SHALL konfigurierbar sein und dokumentiert werden.

#### Scenario: Chip vom Fringe
- WHEN ein Schlag aus 12 m neben dem Grün erfolgt
- THEN zählt er zu Short Game

### Requirement: Nur bei vollständigen Daten
SG SHALL nur für Schläge berechnet werden, bei denen Start- und End-Lie sowie Distanz zum Loch vorliegen. Runden unterhalb der Abdeckungsschwelle (Default 90 % der Schläge) SHALL keine Rundensumme erhalten; es SHALL stattdessen eine Teilabdeckung angezeigt werden.

#### Scenario: Lückenhafte Runde
- WHEN nur 60 % der Schläge Lie haben
- THEN zeigt das System keine SG-Summe, sondern „60 % Abdeckung" und optional SG nur für abgedeckte Kategorien mit Kennzeichnung

### Requirement: Range-Daten ausgeschlossen
Range-/Practice-Sessions SHALL von SG ausgenommen sein und niemals in SG-Kennzahlen einfließen; simulierte Platzrunden SHALL separat als „simuliert" ausgewiesen werden.

#### Scenario: Range-Anfrage
- WHEN `get_strokes_gained` für eine Range-Session aufgerufen wird
- THEN antwortet das System mit „nicht anwendbar" und Begründung

### Requirement: Unsicherheit
SG-Aggregate SHALL mit Stichprobengröße und Konfidenzintervall (Bootstrap über Runden) ausgegeben werden; Mindestanzahl Runden konfigurierbar (Default 5).

#### Scenario: Wenige Runden
- WHEN nur 3 Runden vorliegen
- THEN erscheint der Wert mit Warnung „geringe Datenbasis"

### Requirement: Golden-Tests
Die SG-Berechnung SHALL durch Golden-Tests mit handgerechneten Beispielrunden und fixierter Baseline abgesichert sein.

#### Scenario: Regression
- WHEN die Berechnung geändert wird und ein Golden-Wert abweicht
- THEN schlägt der Test fehl
