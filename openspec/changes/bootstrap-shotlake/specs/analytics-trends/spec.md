# analytics-trends

## ADDED Requirements

### Requirement: Rolling Windows
Das System SHALL Kennzahlen als Rolling Windows (nach Anzahl Runden/Sessions oder Tagen, konfigurierbar) berechnen.

#### Scenario: Letzte 10 Runden
- WHEN Fenster „10 Runden" gewählt ist
- THEN zeigt der Trend je Runde den Mittelwert der jeweils letzten 10

### Requirement: Zeitraumvergleich A vs. B
Das System SHALL zwei frei wählbare Zeiträume (oder „seit letztem Sync" vs. davor) vergleichen: Differenz, relative Änderung, Konfidenzintervall der Differenz (Bootstrap/Welch), n je Zeitraum, Einschätzung „kein belegbarer Unterschied / wahrscheinlich besser / schlechter".

#### Scenario: Neue Trackman-Sim
- WHEN der Nutzer nach dem Sync die neuen Runden (A) mit den 10 davor (B) vergleicht
- THEN erhält er je Kennzahl Δ mit 95-%-CI, n und Hinweis, ob der Unterschied belastbar ist

### Requirement: Mindest-Stichprobe und Warnung
Jede Trendaussage SHALL die Stichprobengröße nennen; unterhalb des Minimums (konfigurierbar) SHALL eine Warnung erscheinen und keine Richtungsaussage („besser/schlechter") getroffen werden.

#### Scenario: n = 2 Runden
- WHEN Zeitraum A nur 2 Runden enthält
- THEN lautet die Aussage „zu wenig Daten" statt Trend

### Requirement: Vergleichbarkeit
Vergleiche SHALL nur Gleiches mit Gleichem verrechnen (gleiche Quelle/Messmethode/real vs. simuliert, optional gleicher Platz/Schläger); Mischungen SHALL kenntlich gemacht werden.

#### Scenario: Real vs. Sim
- WHEN A reale und B simulierte Runden enthält
- THEN erscheint eine Warnung und die Standardansicht trennt beide

### Requirement: Kennzahlenkatalog
Mindestens SHALL verfügbar sein: Score, Score rel. Par, Score Differential (ohne berechneten Handicap-Index), SG je Kategorie, FIR, GIR, Scrambling, Putts/Runde, Putts/GIR, Carry/Total je Schläger, Dispersion, Gapping.

#### Scenario: Auswahl
- WHEN der Nutzer eine Kennzahl wählt
- THEN werden Trend, Fenster und Vergleich einheitlich bereitgestellt

### Requirement: Insights
Das System SHALL automatische Hinweise erzeugen, wo Schläge verloren gehen (z. B. größter negativer SG-Bereich, Rechts-Bias eines Schlägers, Gapping-Lücken), jeweils mit Effektgröße, Unsicherheit (CI/n) und Verweis auf zugrunde liegende Daten; Hinweise SHALL deterministisch und regelbasiert, nicht erfunden sein.

#### Scenario: Putting-Verlust
- WHEN SG Putting über ≥ 5 Runden im Mittel −1,2 (CI −1,8…−0,6) ist
- THEN lautet der Insight „Putting kostet ca. 1,2 Schläge/Runde (95 % CI …)", mit Link zu den Runden

#### Scenario: Unsicher
- WHEN das CI den Nullwert einschließt
- THEN wird der Insight als „unsicher" markiert oder unterdrückt

### Requirement: Annotationen im Trend
Notizen (Equipment/Swing/Coach) SHALL als datierte Marker in Trends angezeigt werden; Vorher/Nachher-Vergleich an einem Marker SHALL per Klick möglich sein.

#### Scenario: Coach-Änderung
- WHEN eine Notiz am 2026-06-01 existiert
- THEN kann der Nutzer „vor vs. nach" für ausgewählte Kennzahlen vergleichen
