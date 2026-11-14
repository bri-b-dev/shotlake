# analytics-handicap

Umfang: Score, Rundenstatistik und **Score Differential** je Runde sowie die Kennzeichnung, ob eine Runde handicap-relevant ist. Ein Handicap-Index wird in Shotlake **nicht** berechnet oder gepflegt (Entscheidung 2026-11-14); Caps, 26,5-Bremse, Beste-8-von-20 und ESR sind nicht im Umfang.

## ADDED Requirements

### Requirement: Score und Rundenstatistik
Das System SHALL je Runde berechnen: Brutto-Score, Netto-Score, Schläge relativ Par, Putts, FIR, GIR, Scrambling, Strafschläge; Quote nur, wenn die zugrunde liegenden Daten erfasst wurden (sonst NULL).

#### Scenario: Keine Fairway-Info
- WHEN eine Quelle FIR nicht liefert
- THEN ist FIR NULL, nicht 0 %

### Requirement: Handicap-Index zum Spielzeitpunkt als Eingabe
Eine Runde SHALL ein optionales Feld `handicap_index_at_play` tragen, das aus der Quelle (z. B. `playerHandicap` in Garmin-Scorecards, Handicap-Angaben aus Trackman) übernommen oder manuell eingetragen wird. Shotlake SHALL diesen Wert nur als Eingabe für Course Handicap, 9-Loch-Anteil und Netto-Doppelbogey verwenden und SHALL daraus keinen Index-Verlauf ableiten.

#### Scenario: Wert aus Quelle
- WHEN eine Garmin-Scorecard `playerHandicap` liefert
- THEN wird er als `handicap_index_at_play` mit Quellenangabe gespeichert und bleibt manuell überschreibbar

### Requirement: Adjusted Gross Score
Der AGS SHALL aus Lochscores mit Höchstscore Netto-Doppelbogey (Par + 2 + erhaltene Vorgabeschläge) berechnet werden. Die Vorgabeschläge SHALL aus dem Course Handicap (`HI × Slope / 113 + (Course Rating − Par)`, gerundet) und dem Lochindex abgeleitet werden; dafür sind `handicap_index_at_play`, Rating, Slope, Par und Lochindizes nötig. Fehlt eine dieser Angaben, SHALL der AGS manuell eingebbar sein; ohne AGS ist das Differential nicht berechenbar.

#### Scenario: Zu hoher Lochscore
- WHEN ein Spieler auf einem Par 4 ohne Vorgabeschlag 9 spielt
- THEN zählt für AGS maximal 6

#### Scenario: Fehlende Lochscores
- WHEN für eine Runde Lochscores fehlen und kein manueller AGS vorliegt
- THEN steht `score_differential` auf NULL mit Grund „AGS nicht ermittelbar"

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
Für 9-Loch-Runden SHALL das Differential als Summe aus dem gespielten Teil `(113 / Slope₉) × (AGS₉ − Rating₉)` und dem erwarteten Differential der nicht gespielten neun Löcher `(HI × 1,04 + 2,4) / 2` berechnet werden, abzüglich `0,5 × PCC`, mit `HI = handicap_index_at_play`. Ohne `handicap_index_at_play` SHALL das Differential NULL sein.

#### Scenario: 9 Löcher mit Index
- WHEN eine 9-Loch-Runde mit Rating/Slope des 9-Loch-Tees und HI 20,0 vorliegt
- THEN ist der erwartete Anteil der übrigen neun Löcher (20,0 × 1,04 + 2,4) / 2 = 11,6 und wird zum gespielten Teil addiert

#### Scenario: Teilrunde mit 11 Löchern
- WHEN eine Runde nur 11 Löcher hat
- THEN ist sie weder 9- noch 18-Loch-Runde im Sinne der Formel und erhält kein Differential

### Requirement: Handicap-Relevanz je Runde
Jede Runde SHALL ein Feld `handicap_relevant` (ja/nein) besitzen, das der Nutzer pro Runde setzen und ändern kann. Ohne Nutzerentscheidung SHALL das System einen **Vorschlag** anzeigen (ja, wenn: reale, abgeschlossene Runde mit mindestens 9 gespielten Löchern, Rating/Slope vorhanden, nicht von der Quelle ausgeschlossen, nicht simuliert; Quellenangaben wie Arccos `includedInLatestHandicap` oder Trackman `isInHcp` SHALL als Hinweis angezeigt werden). Die Nutzerentscheidung hat Vorrang, wird persistiert und bleibt bei Re-Import erhalten. Das Kennzeichen SHALL keinen Einfluss auf die Berechnung des Differentials haben, sondern nur auf Filter, Anzeige und Export.

#### Scenario: Nutzer markiert Runde als relevant
- WHEN der Nutzer eine Runde als handicap-relevant setzt, obwohl der Vorschlag „nein" lautete
- THEN bleibt die Entscheidung gespeichert und ein Re-Import überschreibt sie nicht

#### Scenario: Simulierte Runde
- WHEN eine Trackman-Course-Sim-Runde vorliegt
- THEN lautet der Vorschlag „nein", der Nutzer kann ihn überschreiben, und das Differential wird dennoch berechnet

#### Scenario: Filter
- WHEN die Differential-Ansicht auf „nur handicap-relevant" gefiltert ist
- THEN erscheinen ausschließlich Runden mit `handicap_relevant=true`

### Requirement: Differential-Verlauf
Das System SHALL Differentials als Zeitreihe bereitstellen (alle Runden, handicap-relevante hervorgehoben) und für Auswertungen aus `analytics-trends` verfügbar machen.

#### Scenario: Verlauf
- WHEN der Nutzer den Differential-Verlauf öffnet
- THEN sieht er alle Runden mit Differential, relevante hervorgehoben, nicht berechenbare mit Grund

### Requirement: Golden-Tests Differential
Differential-, AGS-, PCC- und 9-Loch-Berechnung SHALL durch Golden-Tests mit handgerechneten Beispielen abgesichert sein; die Rechenfälle der „handycap"-App SHALL als Vorlage dienen, wobei die PCC-Skalierung bewusst offiziell umgesetzt ist.

#### Scenario: Idempotenz
- WHEN die Berechnung zweimal läuft
- THEN sind die Ergebnisse identisch
