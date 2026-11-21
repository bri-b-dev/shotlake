# frontend

## ADDED Requirements

### Requirement: Dashboard
Das Dashboard SHALL zeigen: Score-Differential-Verlauf (handicap-relevante Runden hervorgehoben; je Punkt Tooltip mit Datum, Platz, Brutto, Differential, gleitendem Durchschnitt und Relevanz, Klick öffnet die Runde; kein berechneter Handicap-Index), letzter Score, SG-Kategorien (mit Abdeckung/Unsicherheit), Trend einer wählbaren Kennzahl, Status der Quellen.

#### Scenario: Wenig Daten
- WHEN SG nicht berechenbar ist
- THEN zeigt die Kachel „nicht genug Daten" mit Grund statt 0

### Requirement: Schläger-Detail
Es SHALL eine Schlägerseite geben mit Länge (Carry/Total), Streuung (Scatter + Ellipse), Gapping und Trend, filterbar nach Quelle/Messmethode/Zeitraum. Die Schlagdistanz auf dem Platz SHALL als Boxplot je Schläger erscheinen (Kasten Q1 bis Q3, Antennen bis 1,5 × Kastenbreite, Ausreißer als Punkte; der Median steht nur im Tooltip). Die Streuung SHALL beide Achsen mit Zahlen und Einheit (m/yd) beschriften und je Punkt einen Tooltip mit Länge und seitlicher Abweichung zeigen.

#### Scenario: Filter
- WHEN der Nutzer Quelle „Mevo+" wählt
- THEN beziehen sich alle Diagramme nur darauf

#### Scenario: Zeitraum („seitdem ich x mache")
- WHEN der Nutzer ein Datum bei „Seit" (und optional „Bis") setzt
- THEN gelten Platzschläge, Range-Tabelle und Streuung nur für diesen Zeitraum, der Filter bleibt beim Wechsel von Quelle, Einheit und Schläger erhalten, ein ungültiges Datum wird ignoriert

### Requirement: Runde/Session-Detail
Es SHALL eine Detailseite für Runde/Session geben mit Lochübersicht, Schlägen, SG-Aufschlüsselung (falls möglich), verknüpften Quellen, Provenienz und Dedup-Status.

#### Scenario: Verknüpfte Runde
- WHEN eine Runde aus Garmin und Arccos stammt
- THEN werden beide Quellen und führende Felder angezeigt

### Requirement: Quellen & Sync
Es SHALL eine Seite geben mit Status je Quelle, letztem Sync, Risiko-Hinweis, Sync-Button (inkrementell oder vollständig, im Hintergrund, höchstens ein Sync gleichzeitig), Prüfen der Zugangsdaten, Datei-Upload (für File-Modus) und Import-Report mit Delta (neu/geändert/Duplikate/Fehler).

#### Scenario: Sync auslösen
- WHEN der Nutzer „Sync" für Trackman klickt
- THEN sieht er Fortschritt, danach den Report und einen Link „Vergleich mit davor"

### Requirement: Zugangsdaten und Zugriffsschutz
Die Oberfläche SHALL ohne Terminal bedienbar sein. Sie SHALL beim ersten Start ein Passwort verlangen (mindestens 10 Zeichen, gehasht, Sitzungs-Cookie `HttpOnly`/`SameSite=Strict`, Login-Sperre nach Fehlversuchen) und danach jede Seite außer den statischen Dateien schützen; fremde `Origin`-Header bei POST SHALL abgelehnt werden. Zugangsdaten für BeBrassie, Arccos und Trackman SHALL auf einer Einstellungsseite verschlüsselt im Datenverzeichnis gespeichert werden (Schlüssel aus `SHOTLAKE_SECRET_KEY` oder Schlüsseldatei mit Rechten 0600). Gespeicherte Geheimnisse SHALL nie wieder angezeigt, geloggt oder in Meldungen genannt werden; leere Felder SHALL bestehende Werte unverändert lassen. Umgebungsvariablen SHALL Vorrang haben.

#### Scenario: Erster Start
- WHEN die Oberfläche ohne gesetztes Passwort aufgerufen wird
- THEN führt jede Seite auf die Passwortvergabe, danach ist die Nutzerin angemeldet

#### Scenario: Zugangsdaten eintragen und prüfen
- WHEN die Nutzerin Zugangsdaten speichert und „Prüfen" wählt
- THEN meldet die Oberfläche Erfolg oder die Fehlerklasse (abgelehnt, nicht erreichbar), nie den Wert

### Requirement: Vergleich A/B
Es SHALL eine Vergleichsansicht geben mit Zeitraumwahl A/B, Kennzahlen-Δ, Konfidenzintervallen, n und Warnungen.

#### Scenario: Schnellvergleich
- WHEN der Nutzer aus dem Import-Report springt
- THEN sind A = neue Daten und B = vorheriger Zeitraum vorbelegt

### Requirement: Handicap-Relevanz setzen
Die Runde-Detailansicht SHALL das Kennzeichen „handicap-relevant" (ja/nein, Standard nein) mit den formalen Kriterien als Hinweis anzeigen und vom Nutzer änderbar machen, in der Rundenliste mit einem Klick umschaltbar; ebenso `handicap_index_at_play`, PCC, AGS und die Platzwerte Rating/Slope. Fachbegriffe wie AGS SHALL im Klartext erklärt sein.

#### Scenario: Umschalten
- WHEN der Nutzer den Schalter „handicap-relevant" umlegt
- THEN wird die Entscheidung gespeichert und der Differential-Verlauf aktualisiert

### Requirement: Notizen
Notizen SHALL angelegt, bearbeitet, gelöscht und Runden, Schlägern, Quellen oder Zeiträumen (gültig ab/bis) zugeordnet werden können; Löschen SHALL eine Bestätigung verlangen. Notiztexte SHALL ausschließlich als Text dargestellt werden. Notizen SHALL als Marker im Differential-Verlauf erscheinen, ein Klick auf einen Marker SHALL einen Vorher/Nachher-Vergleich öffnen (Zeit davor gegen Zeit danach, je Kennzahl mit n, Differenz, Intervall und Einschätzung).

#### Scenario: Notiz anlegen
- WHEN der Nutzer eine Equipment-Notiz mit Datum speichert
- THEN erscheint sie als Marker in Trends

### Requirement: Vorher/Nachher-Vergleich an einer Notiz
Die Vergleichsansicht SHALL für ein Datum (z. B. aus einer Notiz) die Beobachtungen davor (A = danach, B = davor) je Quelle vergleichen und eine Einschätzung nur ab dem Mindest-n geben.

#### Scenario: Marker anklicken
- WHEN der Nutzer einen Marker im Verlauf anklickt
- THEN sieht er für mehrere Kennzahlen Δ, 95-%-Intervall, n je Zeitraum und die Einschätzung

### Requirement: Dedup-Review
Vorgeschlagene Matches SHALL in einer Review-Ansicht bestätigt, getrennt oder zusammengeführt werden können.

#### Scenario: Match ablehnen
- WHEN der Nutzer ablehnt
- THEN wird der Match dauerhaft verworfen

### Requirement: Mobil, schnell, Themes
Das Frontend SHALL ab 360 px Breite nutzbar sein, Dark/Light (System-Default, umschaltbar) unterstützen, Einheiten m/yd umschaltbar anbieten und Seiten in < 1 s (lokal, typische Datenmenge) rendern. Jede Antwort SHALL die Bearbeitungszeit im Header `Server-Timing` ausweisen; rechenintensive Ergebnisse (Bootstrap-Intervalle) SHALL bei gleichen Eingaben nicht erneut berechnet werden. Barrierefreiheit (Kontrast, Tastatur) SHALL beachtet werden.

#### Scenario: Smartphone
- WHEN das Dashboard auf 390 px Breite geöffnet wird
- THEN gibt es kein horizontales Scrollen

### Requirement: Datenehrlichkeit in der UI
Jede Kennzahl SHALL n, Quelle/Messmethode und ggf. Warnung anzeigen; NULL SHALL nie als 0 dargestellt werden.

#### Scenario: Fehlender Wert
- WHEN Spin NULL ist
- THEN zeigt die UI „–"

### Requirement: Sicherung in der Oberfläche
Die Einstellungen SHALL das Herunterladen einer Sicherung (optional mit Passwort), das Herunterladen des Schlüssels nach erneuter Eingabe des Passworts der Oberfläche und das Wiederherstellen per Datei-Upload mit ausdrücklicher Bestätigung bieten. Während eines Syncs SHALL beides abgelehnt werden. Passwörter und Schlüssel SHALL nie in Meldungen oder Logs stehen.

#### Scenario: Schlüssel nur mit Passwort
- WHEN der Schlüssel ohne korrektes Passwort der Oberfläche angefordert wird
- THEN gibt die Oberfläche ihn nicht aus
