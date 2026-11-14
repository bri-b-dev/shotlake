# frontend

## ADDED Requirements

### Requirement: Dashboard
Das Dashboard SHALL zeigen: Score-Differential-Verlauf (handicap-relevante Runden hervorgehoben; kein berechneter Handicap-Index), letzter Score, SG-Kategorien (mit Abdeckung/Unsicherheit), Trend einer wählbaren Kennzahl, Status der Quellen.

#### Scenario: Wenig Daten
- WHEN SG nicht berechenbar ist
- THEN zeigt die Kachel „nicht genug Daten" mit Grund statt 0

### Requirement: Schläger-Detail
Es SHALL eine Schlägerseite geben mit Länge (Carry/Total), Streuung (Scatter + Ellipse), Gapping und Trend, filterbar nach Quelle/Messmethode/Zeitraum.

#### Scenario: Filter
- WHEN der Nutzer Quelle „Mevo+" wählt
- THEN beziehen sich alle Diagramme nur darauf

### Requirement: Runde/Session-Detail
Es SHALL eine Detailseite für Runde/Session geben mit Lochübersicht, Schlägen, SG-Aufschlüsselung (falls möglich), verknüpften Quellen, Provenienz und Dedup-Status.

#### Scenario: Verknüpfte Runde
- WHEN eine Runde aus Garmin und Arccos stammt
- THEN werden beide Quellen und führende Felder angezeigt

### Requirement: Quellen & Sync
Es SHALL eine Seite geben mit Status je Quelle, letztem Sync, Risiko-Hinweis, Sync-Button, Datei-Upload (für File-Modus) und Import-Report mit Delta (neu/geändert/Duplikate/Fehler).

#### Scenario: Sync auslösen
- WHEN der Nutzer „Sync" für Trackman klickt
- THEN sieht er Fortschritt, danach den Report und einen Link „Vergleich mit davor"

### Requirement: Vergleich A/B
Es SHALL eine Vergleichsansicht geben mit Zeitraumwahl A/B, Kennzahlen-Δ, Konfidenzintervallen, n und Warnungen.

#### Scenario: Schnellvergleich
- WHEN der Nutzer aus dem Import-Report springt
- THEN sind A = neue Daten und B = vorheriger Zeitraum vorbelegt

### Requirement: Handicap-Relevanz setzen
Die Runde-Detailansicht SHALL das Kennzeichen „handicap-relevant" (ja/nein) mit Vorschlag und Begründung anzeigen und vom Nutzer änderbar machen; ebenso `handicap_index_at_play` und PCC.

#### Scenario: Umschalten
- WHEN der Nutzer den Schalter „handicap-relevant" umlegt
- THEN wird die Entscheidung gespeichert und der Differential-Verlauf aktualisiert

### Requirement: Notizen
Notizen SHALL angelegt, bearbeitet, gelöscht und Schlägern/Sessions/Zeiträumen zugeordnet werden können.

#### Scenario: Notiz anlegen
- WHEN der Nutzer eine Equipment-Notiz mit Datum speichert
- THEN erscheint sie als Marker in Trends

### Requirement: Dedup-Review
Vorgeschlagene Matches SHALL in einer Review-Ansicht bestätigt, getrennt oder zusammengeführt werden können.

#### Scenario: Match ablehnen
- WHEN der Nutzer ablehnt
- THEN wird der Match dauerhaft verworfen

### Requirement: Mobil, schnell, Themes
Das Frontend SHALL ab 360 px Breite nutzbar sein, Dark/Light (System-Default, umschaltbar) unterstützen, Einheiten m/yd umschaltbar anbieten und Seiten in < 1 s (lokal, typische Datenmenge) rendern. Barrierefreiheit (Kontrast, Tastatur) SHALL beachtet werden.

#### Scenario: Smartphone
- WHEN das Dashboard auf 390 px Breite geöffnet wird
- THEN gibt es kein horizontales Scrollen

### Requirement: Datenehrlichkeit in der UI
Jede Kennzahl SHALL n, Quelle/Messmethode und ggf. Warnung anzeigen; NULL SHALL nie als 0 dargestellt werden.

#### Scenario: Fehlender Wert
- WHEN Spin NULL ist
- THEN zeigt die UI „–"
