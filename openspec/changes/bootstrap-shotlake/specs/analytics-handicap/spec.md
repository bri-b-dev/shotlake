# analytics-handicap

Umfang: Score, Rundenstatistik und **Score Differential** je Runde sowie die Kennzeichnung, ob eine Runde handicap-relevant ist. Ein Handicap-Index wird in Shotlake **nicht** berechnet oder gepflegt (Entscheidung 2026-11-14); Caps, 26,5-Bremse, Beste-8-von-20 und ESR sind nicht im Umfang.

## ADDED Requirements

### Requirement: Score und Rundenstatistik
Das System SHALL je Runde berechnen: Brutto-Score, Netto-Score, Schläge relativ Par, Putts, FIR, GIR, Scrambling, Strafschläge; Quote nur, wenn die zugrunde liegenden Daten erfasst wurden (sonst NULL).

#### Scenario: Keine Fairway-Info
- WHEN eine Quelle FIR nicht liefert
- THEN ist FIR NULL, nicht 0 %

### Requirement: Handicap-Index zum Spielzeitpunkt nur als manuelle Eingabe
Eine Runde SHALL ein optionales, **manuell gesetztes** Feld `handicap_index_at_play` tragen. Handicap-Angaben aus Quellen (z. B. `playerHandicap` in Garmin-Scorecards, Trackman-Handicap) SHALL NOT übernommen oder ausgewertet werden. Der Wert dient nur als Eingabe für Course Handicap, Netto-Höchstscore, 9-Loch-Anteil und Netto-Score; Shotlake SHALL daraus keinen Index-Verlauf ableiten.

#### Scenario: Quelle liefert ein Handicap
- WHEN eine Garmin-Scorecard `playerHandicap` enthält
- THEN wird der Wert ignoriert und `handicap_index_at_play` bleibt NULL, bis der Nutzer ihn setzt

### Requirement: Adjusted Gross Score
Der AGS SHALL aus Lochscores mit Höchstscore Netto-Doppelbogey (Par + 2 + erhaltene Vorgabeschläge) berechnet werden. Die Vorgabeschläge SHALL aus dem Course Handicap (`HI × Slope / 113 + (Course Rating − Par)`, gerundet) und dem Lochindex abgeleitet werden; dafür sind `handicap_index_at_play`, Rating, Slope, Par und Lochindizes nötig. Fehlt `handicap_index_at_play`, SHALL bei 18-Loch-Runden das **unbereinigte Brutto** als AGS dienen (Kennzeichnung `ags_source = gross`), damit ein Verlauf möglich bleibt; der AGS SHALL außerdem manuell eingebbar sein. Fehlen Par oder Lochindizes, ist das Differential nicht berechenbar.

#### Scenario: Zu hoher Lochscore
- WHEN ein Spieler auf einem Par 4 ohne Vorgabeschlag 9 spielt
- THEN zählt für AGS maximal 6

#### Scenario: Fehlende Lochscores
- WHEN für eine Runde Lochscores fehlen und kein manueller AGS vorliegt
- THEN steht `score_differential` auf NULL mit Grund „AGS nicht ermittelbar"

#### Scenario: Kein Index gesetzt
- WHEN bei einer 18-Loch-Runde kein `handicap_index_at_play` gesetzt ist
- THEN wird das Differential aus dem unbereinigten Brutto berechnet und als `gross` gekennzeichnet

### Requirement: Score Differential
Das Score Differential SHALL nach der offiziellen Formel `(113 / Slope) × (AGS − Course Rating − PCC)` berechnet und auf eine Dezimalstelle gerundet werden. Course Rating und Slope SHALL aus dem gespielten Tee stammen (z. B. `teeBoxRating`/`teeBoxSlope` bei Garmin). Das PCC ist Default 0 und manuell setzbar (Bereich −1,0 bis +3,0). Runden ohne Rating/Slope SHALL kein Differential erhalten und als solche angezeigt werden. Die abweichende Rechenweise der „handycap"-App (`(AGS − CR) × 113/Slope − PCC`) SHALL NOT übernommen werden.

#### Scenario: Beispiel ohne PCC
- WHEN AGS 90, Course Rating 71,2, Slope 130, PCC 0
- THEN ist das Differential 113/130 × 18,8 = 16,3

#### Scenario: PCC wird mitskaliert
- WHEN AGS 90, Course Rating 71,2, Slope 130, PCC +1,0
- THEN ist das Differential 113/130 × 17,8 = 15,5 (nicht 16,3 − 1,0 = 15,3)

#### Scenario: Kein Rating
- WHEN der Tee keine Rating/Slope-Angabe hat
- THEN ist `score_differential` NULL und die UI nennt den Grund

### Requirement: 9-Loch-Runden
Für 9-Loch-Runden mit hinterlegtem 9-Loch-Rating und gesetztem `handicap_index_at_play` SHALL das Differential nach der aktuellen WHS-Regel berechnet werden: `113 / Slope₉ × (AGS₉ − Rating₉ − PCC) + (0,52 × Index + 1,2)` (9-Loch-Differential plus erwartetes Differential der übrigen neun Löcher, je auf eine Dezimale gerundet). Fehlt eines von beiden, SHALL das Differential auf 18 Löcher **hochgerechnet** werden: der AGS der neun Löcher wird verdoppelt und gegen das 18-Loch-Rating und den Slope des gespielten Tees gerechnet (`113 / Slope × (2 × AGS₉ − Rating − PCC)`). Liegen 9-Loch-Werte des Tees vor (manuell hinterlegt), SHALL stattdessen das doppelte 9-Loch-Rating und der 9-Loch-Slope verwendet werden. Der Höchstscore (Netto-Doppelbogey) SHALL bei 9 Löchern nur mit 9-Loch-Rating und gesetztem `handicap_index_at_play` angewendet werden, sonst gilt das unbereinigte Brutto. Hochgerechnete Differentials SHALL als solche gekennzeichnet sein. Die Verdopplung ist eine Näherung ohne Handicap-Index.

#### Scenario: 9 Löcher mit Index (WHS)
- WHEN eine Front-9-Runde mit AGS 57, 9-Loch-Rating 37,7, Slope 135 und Handicap-Index 39,5 vorliegt
- THEN ist das Differential 113/135 × (57 − 37,7) + (0,52 × 39,5 + 1,2) = 16,2 + 21,7 = 37,9

#### Scenario: 9 Löcher ohne 9-Loch-Rating
- WHEN eine 9-Loch-Runde mit 45 Schlägen auf einem Tee mit Rating 71,2 und Slope 130 vorliegt
- THEN ist das Differential 113/130 × (90 − 71,2) = 16,3 und als hochgerechnet gekennzeichnet

#### Scenario: 9 Löcher mit 9-Loch-Rating
- WHEN zusätzlich 9-Loch-Rating 35,8 und Slope 125 hinterlegt sind
- THEN gilt 113/125 × (90 − 71,6) für das Differential

#### Scenario: Teilrunde mit 11 Löchern
- WHEN eine Runde nur 11 Löcher hat
- THEN ist sie weder 9- noch 18-Loch-Runde im Sinne der Formel und erhält kein Differential

### Requirement: Handicap-Relevanz je Runde
Jede Runde SHALL ein Feld `handicap_relevant` (ja/nein) besitzen, das der Nutzer pro Runde setzen und ändern kann. **Standard ist nein**: ohne Nutzerentscheidung gilt eine Runde als nicht handicap-relevant, unabhängig von der Quelle (Entscheidung 2026-11-14). Das System SHALL formale Kriterien nur als **Hinweis** anzeigen (reale, abgeschlossene Runde mit mindestens 9 gespielten Löchern, Rating/Slope vorhanden, nicht von der Quelle ausgeschlossen, nicht simuliert) und SHALL daraus nichts automatisch setzen; Quellenangaben wie Arccos `includedInLatestHandicap` oder Trackman `isInHcp` SHALL höchstens als Hinweis erscheinen. Die Nutzerentscheidung wird persistiert und bleibt bei Re-Import erhalten. Das Kennzeichen SHALL keinen Einfluss auf die Berechnung des Differentials haben, sondern nur auf Filter, Anzeige und Export.

#### Scenario: Neue Runde
- WHEN eine Runde importiert wird, für die der Nutzer nichts entschieden hat
- THEN ist sie nicht handicap-relevant, auch wenn die Quelle Rating und Slope liefert

#### Scenario: Nutzer markiert Runde als relevant
- WHEN der Nutzer eine Runde als handicap-relevant setzt
- THEN bleibt die Entscheidung gespeichert und ein Re-Import überschreibt sie nicht

#### Scenario: Filter
- WHEN die Differential-Ansicht auf „nur handicap-relevant" gefiltert ist
- THEN erscheinen ausschließlich Runden, die der Nutzer als relevant gesetzt hat

### Requirement: Platzwerte (Rating/Slope) setzen
Der Nutzer SHALL Course Rating und Slope je Platz und Tee für 18 Löcher sowie optional für Vorder- und Hinterneun hinterlegen können. Hinterlegte Werte SHALL Vorrang vor Quellenwerten haben, für alle Runden auf diesem Platz mit diesem Tee gelten und bei Re-Import erhalten bleiben. Quellen ohne Tee-Angabe SHALL den Tee „unbekannt" verwenden. Eingaben SHALL auf plausible Bereiche geprüft werden.

#### Scenario: Quelle ohne Rating
- WHEN eine Arccos-Runde kein Rating/Slope liefert und der Nutzer sie für den Platz hinterlegt
- THEN erhält die Runde ein Score Differential

### Requirement: Differential-Verlauf
Das System SHALL Differentials als Zeitreihe bereitstellen (alle Runden, handicap-relevante hervorgehoben) und einen **gleitenden Durchschnitt** über die letzten N Runden mit Differential berechnen (Standard N = 5, konfigurierbar, optional nur handicap-relevante Runden). Vor Erreichen von N Werten SHALL der Durchschnitt NULL sein. Ein Handicap-Index wird daraus nicht abgeleitet. Die Zeitreihe SHALL für Auswertungen aus `analytics-trends` verfügbar sein.

#### Scenario: Verlauf
- WHEN der Nutzer den Differential-Verlauf öffnet
- THEN sieht er alle Runden mit Differential, relevante hervorgehoben, nicht berechenbare mit Grund, und den gleitenden Durchschnitt

#### Scenario: Zu wenige Runden
- WHEN erst 3 Differentials vorliegen und N = 5 gilt
- THEN ist der gleitende Durchschnitt NULL

### Requirement: Golden-Tests Differential
Differential-, AGS-, PCC- und 9-Loch-Berechnung SHALL durch Golden-Tests mit handgerechneten Beispielen abgesichert sein; die Rechenfälle der „handycap"-App SHALL als Vorlage dienen, wobei die PCC-Skalierung bewusst offiziell umgesetzt ist.

#### Scenario: Idempotenz
- WHEN die Berechnung zweimal läuft
- THEN sind die Ergebnisse identisch
