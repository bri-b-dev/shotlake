# analytics-distance

## ADDED Requirements

### Requirement: Länge je Schläger
Das System SHALL je Schläger Carry und Total (Mittel, Median, P10/P90, n) über wählbaren Zeitraum berechnen.

#### Scenario: Eisen 7
- WHEN der Nutzer Eisen 7 über 90 Tage abfragt
- THEN liefert das System Median-Carry, P10/P90, n und Quelle(n)

### Requirement: Trennung nach Quelle und Messmethode
Distanz- und Streuungskennzahlen SHALL standardmäßig getrennt je Quelle und Messmethode (Radar, Kamera, Sensor, Sim) ausgewiesen werden; kombinierte Sicht SHALL nur explizit gewählt werden und die Mischung kennzeichnen.

#### Scenario: Zwei Geräte
- WHEN Driver-Daten von Trackman und Mevo+ vorliegen
- THEN werden beide nebeneinander angezeigt, nicht gemittelt

### Requirement: Optionaler Kalibrierungsfaktor
Pro Quelle/Schläger SHALL ein Kalibrierungsfaktor (multiplikativ/additiv) zu einer Referenzquelle konfigurierbar sein. Er SHALL nur auf Anzeige/Vergleich wirken, nie auf gespeicherte Rohwerte, und SHALL im UI sichtbar sein.

#### Scenario: Faktor aktiv
- WHEN Faktor 1.02 für Mevo+ Driver gesetzt ist
- THEN zeigen Vergleiche „kalibriert" an, canonical bleibt unverändert

### Requirement: Dispersion
Das System SHALL Streuung berechnen: seitliche Abweichung (Mittel = Bias links/rechts, Standardabweichung), Längenstreuung, 2D-Streuellipse (z. B. 68 %/90 %), Anteil Treffer links/rechts/Ziel-Korridor, bezogen auf Zielrichtung der Quelle.

#### Scenario: Fehlerbild
- WHEN Seitenabweichungen im Mittel 8 m rechts liegen
- THEN meldet das System „Bias rechts" mit Wert, n und Konfidenzintervall (95 %, t-Intervall des Mittelwerts; der Carry-Median bekommt ein verteilungsfreies Intervall über Rangzahlen, beide ab 10 Schlägen, ohne Resampling und damit auch bei sehr vielen Schlägen schnell)

### Requirement: Gapping
Das System SHALL je Schläger Carry-Mediane ordnen und Lücken/Überlappungen zwischen benachbarten Schlägern ausweisen (Differenz, Warnung unter/über konfigurierbaren Schwellen, z. B. < 8 m oder > 18 m).

#### Scenario: Überlappung
- WHEN Eisen 6 und Eisen 7 Median < 4 m auseinander liegen
- THEN wird eine Überlappung markiert

### Requirement: Mindest-Stichprobe
Kennzahlen SHALL nur ohne Warnung erscheinen, wenn n ≥ konfigurierbares Minimum (Default 10 Schläge je Schläger/Quelle); sonst Hinweis „zu wenig Daten".

#### Scenario: n = 4
- WHEN nur 4 Schläge vorliegen
- THEN wird der Wert mit Warnung und ohne Konfidenzaussage gezeigt

### Requirement: Filter
Berechnungen SHALL nach Zeitraum, Quelle, Messmethode, Conditions (indoor/outdoor, Wind, Höhe), Ball und Quality-Flag filterbar sein.

#### Scenario: Nur Outdoor
- WHEN Filter „outdoor" gesetzt ist
- THEN fließen Indoor-Sim-Schläge nicht ein
