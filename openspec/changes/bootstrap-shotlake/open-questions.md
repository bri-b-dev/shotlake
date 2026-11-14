# Offene Fragen & Annahmen (bitte bestätigen)

## Bestätigte Entscheidungen (2026-11-14)
| Thema | Entscheidung | Frage |
|---|---|---|
| SG-Baseline | Standard **Handicap-Band** (Ziel-Handicap konfigurierbar, z. B. aus Arccos `goalHcp`); Tour/Scratch wählbar; eigene dokumentierte Näherungstabellen | 1 |
| SG-Kategorien | Short Game ≤ 50 m vom Grün (nicht auf Grün), Approach darüber, Par-3-Abschlag = Approach; konfigurierbar | 2 |
| SG-Abdeckung | Rundensumme ab **≥ 90 %** Lie-Abdeckung, sonst Teilabdeckung | 3 |
| Sim-Runden | **Getrennt ausweisen**, nie mit realen mischen; Handicap nur aus realen Runden | 4 |
| Datenbank | **DuckDB** (+ Parquet) | 5 |
| Hosting | **Docker Compose lokal/NAS**, nur LAN/VPN; K8s-Manifeste als Zusatz | 6 |
| Zugriff | Kein Login im LAN; MCP über HTTP nur mit Token (oder stdio) | 7 |
| ToS | Inoffizielle APIs **produktiv nutzbar mit Schutzmaßnahmen** (nur eigener Account, niedriger Takt, je Connector abschaltbar, Risiko dokumentiert) | 9 |
| Screenshots | **Nicht im Umfang**; BeBrassie nur über Web-API bzw. manuellen Import | 10, 11 |
| Handicap | **Nur Score Differential** (offizielle Formel, 9-Loch-Regel, PCC manuell) + Kennzeichen „handicap-relevant" je Runde; **kein Handicap-Index**, keine Caps/ESR (ersetzt frühere WHS-Entscheidung) | 12, 12a |
| Einheiten/Kalibrierung | Anzeige Meter (yd umschaltbar); Trackman als Referenz, Kalibrierungsfaktor optional | 14, 15 |
| Historie/Backup | Alles importieren; Backup (DB + raw) in lokales Verzeichnis/NAS, optional verschlüsselt | 17, 18 |
| Secrets | sops (age), verschlüsselte Datei im Repo erlaubt | 8 |
| Eigenständigkeit | Option A, kein Code-Import aus `analytics-platform` | 0 |

**Entschieden (2026-11-14) zu 12a:** (i) PCC offiziell `113/Slope × (AGS − CR − PCC)`; (ii) „Bremse an" und (iii) „Mindestens 1 Runde" sind **hinfällig**, weil Shotlake **keinen Handicap-Index berechnet** – nur Score Differential je Runde und ein Kennzeichen „handicap-relevant" (Vorschlag + Nutzerentscheidung); (iv) kein DGV-PDF. Caps, 26,5-Bremse, Beste-8-von-20 und Index-Verlauf sind nicht im Umfang. Für AGS und 9-Loch-Formel dient `handicap_index_at_play` als optionale Eingabe (aus Quelle oder manuell). *Ursprüngliche Fragen:*
**Aus dem Vergleich mit `../handycap` neu (12a):** (i) PCC-Formel: offiziell `113/Slope × (GBE − CR − PCC)` (Spec) vs. App `(GBE − CR) × 113/Slope − PCC` – welche soll gelten? Annahme: offizielle, App-Abweichung als Hinweis; (ii) DGV-26,5-Bremse: als konfigurierbare Regel, Standard an oder aus? (iii) Mindestanzahl Runden: Standard 3 (WHS) oder 1 wie in der App? (iv) DGV-Scoring-Record-PDF als optionale Referenzquelle?

**Weiterhin offen:** age-Schlüssel-Backup (Frage 8), ~~Pfad zur „handycap"-App (13)~~ (gefunden: `../handycap`), Equipment-Granularität (16: Annahme Ball + Schläger mit Gültigkeitszeitraum), Arccos-Session-Laufzeit (0d), Trackman-Schlagdaten-Abfrage, Bedeutung von Garmin-`UNKNOWN`/`LAYUP` und Einheit-Gegenprüfung an weiteren Runden.

## 0. Grundsatzfrage (neu aus Phase 0)
0. **ENTSCHIEDEN (2026-11-14): Option A – Shotlake ist eigenständig, mit eigener DB.** Poller/Parser aus `analytics-platform` und `arccos-fetch` dienen nur als Referenz (kein Code-Import, keine Laufzeit-Abhängigkeit). Frage 5 (DuckDB) und 6 (Hosting ohne Cluster-Zwang) bleiben zu bestätigen. Ursprüngliche Frage: Verhältnis zu `analytics-platform`. Dort laufen bereits Mevo+- und Arccos-Ingestion (Poller, Bronze/Silver/Gold, Superset) und BeBrassie ist angelegt. Soll Shotlake (A) eigenständig mit eigener DB sein, (B) nur Frontend/MCP/Analytics auf deren Gold-Tabellen (Trino), oder (C) Connectors teilen? Siehe `phase0-findings.md` §0. Empfehlung: A, wenn der Cluster nicht dauerhaft laufen soll; sonst B/C. **Die Antwort entscheidet über Speicher (Frage 5), Hosting (6) und den Umfang von `ingest-connectors`.**
0b. **Arccos-Schlagdetail/Lie**: Das Detail-Endpoint (GPS je Schlag) ist in `docs.md` Abschnitt „GET round" nur mit der **Stats-URL** (Copy-Paste-Fehler, identisch mit „Get stats") dokumentiert, die Antwortstruktur (`holes[].shots[]`) ist aber vorhanden → **Update 2026-11-14:** Das HAR vom neuen Dashboard (`dashboard.arccosgolf.com`) zeigt, dass Arccos die Web-App neu implementiert hat; dort liefert `roundDetailBundle` je Schlag `startTerrain`/`endTerrain` (tee/fairway/rough/sand/green), Distanz zum Grünzentrum und Arccos-SG je Schlag → Lie **ist** verfügbar, selbst berechnete SG damit grundsätzlich möglich (Distanz zur Fahne via `pinLat/pinLng` statt Grünzentrum; kein Fringe/Strafschlag-Terrain). Neue Fragen: Welcher Weg ist der Primärweg – neues Dashboard (Remote Functions, build-abhängige Hashes, Auth unklar) oder alte `api.arccosgolf.com` (im HAR nicht zu sehen, ob noch aktiv)? Ältere Doku (`docs.md`): ein direktes Lie-Feld gab es nicht; dort waren `userStartTerrainOverride` (Bedeutung der Zahlen unbekannt), `isSandUser`, `isNonSandUser`, Start-/End-GPS, `pinLat/pinLong`, `startAltitude` (meist null). Soll SG für Arccos-Runden (i) die von Arccos berechneten SG-Werte übernehmen (Baseline = `goalHcp`) oder (ii) selbst gerechnet werden (Lie müsste aus GPS/Platzgeometrie abgeleitet werden)? Annahme: (i) zuerst, (ii) nur wenn Lie vorliegt.
0d. **ENTSCHIEDEN (2026-11-14):** Alte Arccos-API läuft nicht mehr → Option (iii) entfällt. Speichern von `accessKey/secret/userId` als Secret ist akzeptiert (Option iv), Ablage per **sops** (Frage 8). Session-Laufzeit unbekannt (Nutzerin weiß nicht, wann sie sich zuletzt angemeldet hat) → Live-Test in Phase 0 bleibt nötig; Fallback bleibt manuelles Einlösen. Ursprüngliche Fragestellung: **Arccos-Login per Magic-Link**: Unattended Sync ist nur möglich, wenn die Session lange genug lebt. Optionen: (i) Nutzer löst Magic-Link manuell ein bzw. fügt die Session (Cookie) in Shotlake ein; bei Ablauf Status `auth_required`, Benachrichtigung (Annahme, Standard), (ii) Shotlake liest den Link automatisch per IMAP/Gmail-API aus dem Postfach (benötigt Mail-Zugangsdaten, fragiler, ToS-/Sicherheitsrisiko), (iii) alte API mit Passwort weiternutzen, falls sie noch läuft. **Stand nach `login2`-HAR:** Redirect-Kette bekannt (Tracking-Redirect → `authentication.arccosgolf.com/v1/redeemMagicLink?token=` → `dashboard…/magic-link?accessKey&secret&userId` → Session). Damit gibt es eine vierte Option (iv): `accessKey/secret/userId` einmalig aus dem Redeem-Redirect speichern und Sessions daraus erneuern (Hypothese, Laufzeit unbekannt). Noch offen: Cookie-Name/Laufzeit, Einmaligkeit des Tokens, Gültigkeitsdauer von `accessKey/secret`. Bitte (a) einmal prüfen, wie lange du im Dashboard angemeldet bleibst, und (b) entscheiden, ob (iv) mit Speichern dieser Credentials als Secret akzeptabel ist.
0c. **Mevo+-Dateinamen-Zeitzone**: Annahme Europe/Berlin und 12-h-Format; bitte bestätigen. Garmin/Trackman-Beispieldaten und -Docs liegen nicht vor – bitte liefern oder Phase 0 dafür durchführen.

## Strokes Gained
1. **Baseline**: Welche? Annahme: Standard = Scratch/Handicap-nahe Baseline, wählbar (Tour / Scratch / Handicap-Band). Tour-Daten (Broadie/ShotLink) sind nicht frei lizenziert → Annahme: eigene, dokumentierte Näherungstabellen (z. B. publizierte Näherungen) mit Quellenangabe. Einverstanden? Zielhandicap für Handicap-Baseline?
2. **Kategorie-Grenzen**: Short Game = ≤ 50 m (≈ 50 yd) vom Grün, Approach = > 50 m nicht auf Grün, Tee = erster Schlag Par 4/5; Par 3 Abschlag als Approach? Annahme: Par-3-Abschlag zählt als Approach.
3. **Lie-Datenqualität**: Arccos/Garmin liefern Lie/Distanz je Schlag unterschiedlich zuverlässig – SG nur bei vollständigen Daten. Ab welcher Abdeckung (Annahme: ≥ 90 % der Schläge einer Runde) berechnen?
4. **Trackman Course-Sim**: Soll SG für Simulatorrunden berechnet werden (Lie/Distanz meist vorhanden)? Annahme: ja, aber als eigene Kategorie „simuliert", nicht mit realen Runden vermischt.

## Speicher / Hosting
5. **DB**: DuckDB (Annahme) vs. Postgres. Wird Mehrprozess-Zugriff oder externes BI-Tool gebraucht?
6. **Hosting**: Docker Compose auf Heimserver/NAS oder K8s (k3s?) – welche Umgebung? Zugriff nur LAN/VPN oder auch extern (dann Auth-Proxy nötig)?
7. **Auth**: Annahme: kein Login im LAN, optional Reverse-Proxy-Auth/Token für MCP-HTTP. OK?
8. **Secret-Ablage**: **sops (age)** gewünscht (2026-11-14). Annahmen zur Bestätigung: statische Secrets (Quell-Credentials) in einer sops-verschlüsselten Datei, age-Schlüssel außerhalb des Repos (Datei 0600 bzw. Docker/K8s Secret) und Entschlüsselung erst zur Laufzeit; veränderliche Zustände (Sessions, Tokens, Cursor) liegen getrennt im Datenvolumen, nicht in der sops-Datei, und sind nicht Teil des Klartext-Exports. **Entschieden (2026-11-14):** Die **verschlüsselte** sops-Datei darf im Repo liegen. **Offen:** sops/age ist noch nicht installiert (Setup-Task in Phase 1) und es gibt noch kein Konzept, wo der age-Schlüssel gesichert wird (Passwortmanager/Offline-Kopie?); ohne Schlüssel-Backup sind die Secrets nach Verlust unbrauchbar.

## Quellen / ToS
9a. **Entschieden (2026-11-14):** Trackman geht nur über die inoffizielle Web-API (kein Performance-Studio-Zugang); Garmin-Platz nur über die API; Garmin-Range-Annahmen (Spin rpm, Zeitzone Europe/Berlin, 24-h trotz PM) und Mevo+-Zeitzone bestätigt. Damit sind drei von fünf Quellen (Arccos, Trackman, Garmin-Platz) ToS-riskant → Frage 9 gewinnt an Gewicht.
9. **ToS-Risiko**: Annahme: inoffizielle APIs (Garmin, Arccos, Trackman Web) nur mit eigenem Account, niedriger Takt, per Config abschaltbar, Datei-Export als Primärweg, Risiko je Connector dokumentiert. Akzeptabel, oder inoffizielle Wege komplett ausschließen?
10. **BeBrassie**: Wenn kein Export existiert – manuelle Erfassung oder Screenshot-OCR (Qualität/Kosten)? Annahme: manueller CSV/Formular-Import zuerst.
11. **Screenshot-Modus**: LLM-Extraktion (Cloud-Call = Daten verlassen Rechner) oder lokal/OCR? Annahme: nur lokal, oder explizit pro Import freigegeben.

## Handicap / Domäne
12. **WHS-Details**: Soft/Hard Cap, 9-Loch-Runden, ESR, PCC-Quelle (PCC nur manuell eingebbar – Annahme)? Nur Runden mit Course Rating/Slope zählen. Offizieller Index (DGV) als Referenz zum Abgleich vorhanden?
13. **„handycap"-App**: Pfad/Repo? Annahme: Referenz für Formeln/Testfälle, kein Code-Import.
14. **Messmethoden-Kalibrierung**: Trackman als Referenz (Annahme), Kalibrierungsfaktor je Schläger/Quelle optional?
15. **Einheiten**: Anzeige-Default m oder yd? (Annahme: m, umschaltbar)
16. **Bälle/Equipment**: Wie fein (Ballmodell, Schaft, Loft)? Annahme: Ball + Schläger-Stammdaten mit Gültigkeitszeitraum.
17. **Historie**: Wie weit zurück soll importiert werden (Annahme: alles Verfügbare)?
18. **Backup-Ziel**: Lokales Verzeichnis/NAS/rclone? Verschlüsselung?
