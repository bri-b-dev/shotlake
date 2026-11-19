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
Die Baseline SHALL wählbar sein (Tour, Scratch, Handicap-Band X). Mitgeliefert werden zwei als Näherung gekennzeichnete Handicap-Bänder für Damen: `hcp40w-approx-v1` („Mein Niveau", **Standard**) und `hcp18w-approx-v1` („Ziel Handicap 18"). Jede Baseline SHALL als versionierte Tabelle (Lie × Distanz → erwartete Schläge) mit Quellenangabe, Lizenzhinweis, Status, Ziel-Handicap und Anzeigename im Repo liegen, in sich stimmig sein (ein Abschlag ist nie schlechter als ein Fairwayschlag auf gleicher Distanz) und für ein schwächeres Niveau überall höhere erwartete Schläge ergeben. Jede SG-Ausgabe SHALL die verwendete Baseline-ID nennen. Die Wahl SHALL in Oberfläche (`?b=<ID>`, Umschalter, bleibt in der Navigation erhalten), CLI (`--baseline`, Kennung oder Datei; `baselines` listet sie) und MCP (`baseline`) möglich sein; eine unbekannte Kennung SHALL auf den Standard zurückfallen (Oberfläche) bzw. mit einer Liste der verfügbaren Baselines abgelehnt werden (CLI, MCP).

#### Scenario: Baseline-Wechsel
- WHEN die Baseline gewechselt wird
- THEN werden SG-Werte neu berechnet und die Baseline-ID in der Ausgabe angezeigt

#### Scenario: Umschalter
- WHEN in der Oberfläche von „Mein Niveau" auf „Ziel Handicap 18" gewechselt wird
- THEN zeigen Übersicht, Rundendetail, Hinweise und Vergleich die SG gegen diese Baseline, und die Wahl bleibt beim Navigieren erhalten

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

### Requirement: Standard-Baseline Handicap 18 (Damen)
Die Standard-Baseline SHALL `hcp18w-approx-v1` sein (Handicap-Band 18, Damen), als versionierte TOML-Tabelle im Repo mit `id`, `source`, `license` und `status`. Die Werte SHALL als eigene, ungeprüfte Näherung gekennzeichnet sein (`status = approximation`); fremde Tabellen (Tour/ShotLink) SHALL NOT übernommen werden. Jede SG-Ausgabe SHALL Baseline-ID und Status nennen.

#### Scenario: Baseline ohne Quellenangabe
- WHEN eine Baseline-Datei `source` oder `license` nicht enthält
- THEN lehnt das System sie mit einer Fehlermeldung ab

### Requirement: Vollständigkeit und Putt-Distanz
SG SHALL nur für **vollständig erfasste Löcher** berechnet werden: Zahl der Schläge = Score − Putts − Strafschläge, alle Lagen, Koordinaten und die Fahnenposition vorhanden, kein von der Quelle ausgeschlossener Schlag. Unvollständige Löcher SHALL mit Grund ausgewiesen und nie geschätzt werden; Summen SHALL die Abdeckung (vollständige von allen Löchern) nennen. Liefert die Quelle für das Ende des letzten Schlags auf dem Grün keine echte Ballposition (Ende liegt weniger als 1 m von der Fahnenposition, wie bei Garmin), SHALL die erste Putt-Distanz als unbekannt gelten; dann SHALL der letzte Schlag und die Putts gemeinsam als Kategorie „Aufs Grün + Putts" ausgewiesen und Putten nicht separat bewertet werden.

#### Scenario: Fehlender Schlag
- WHEN ein Loch Score 5 und 2 Putts hat, aber nur 1 Schlag erfasst ist
- THEN erscheint das Loch als unvollständig („1 Schläge erfasst, 3 erwartet") und geht nicht in die Summen ein

#### Scenario: Grünposition entspricht der Fahne
- WHEN das Ende des letzten Schlags 0 m von der Fahnenposition entfernt liegt
- THEN zählt der Schlag samt Putts als „Aufs Grün + Putts" und Putten bleibt für dieses Loch leer
