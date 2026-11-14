# mcp-server

## ADDED Requirements

### Requirement: Gemeinsame Service-Schicht
Der MCP-Server SHALL dieselbe Service-Schicht wie API/Frontend nutzen; Ergebnisse SHALL für gleiche Eingaben identisch sein.

#### Scenario: Konsistenz
- WHEN `get_stats` und die UI denselben Filter nutzen
- THEN stimmen die Werte überein

### Requirement: Trennung read-only / schreibend
Tools SHALL in zwei Gruppen getrennt sein: **read-only**: `list_sources`, `import_report`, `get_stats`, `compare_periods`, `get_club_gapping`, `get_strokes_gained`, `get_insights`; **schreibend**: `sync_source`, `add_note`. Schreibende Tools SHALL separat deaktivierbar sein (Config/Scope) und als `destructive/non-readonly` annotiert werden.

#### Scenario: Read-only-Modus
- WHEN der Server mit `--read-only` startet
- THEN sind `sync_source` und `add_note` nicht registriert

### Requirement: Tool `sync_source`
`sync_source(source, mode=incremental|full)` SHALL einen Sync starten und den Import-Report (neu/geändert/Duplikate/Fehler, Status) liefern; bei laufendem Sync SHALL kein zweiter starten.

#### Scenario: Auth fehlt
- WHEN die Quelle `auth_required` ist
- THEN antwortet das Tool mit klarer Fehlermeldung ohne Secrets

### Requirement: Tool `list_sources`
Das Tool SHALL Quellen mit Modus, Status, letztem Sync, Risiko und Datenumfang liefern.

#### Scenario: Übersicht
- WHEN aufgerufen
- THEN erhält der Aufrufer alle 5 Quellen mit Status

### Requirement: Tool `import_report`
`import_report(source?, run_id?)` SHALL den letzten bzw. gewählten Report liefern.

#### Scenario: Letzter Lauf
- WHEN ohne Parameter aufgerufen
- THEN wird der jüngste Report geliefert

### Requirement: Tool `get_stats`
`get_stats(metric, filters, window)` SHALL Kennzahl mit Wert, n, CI, Quelle/Messmethode und Warnungen liefern.

#### Scenario: Zu wenig Daten
- WHEN n unter Minimum liegt
- THEN enthält die Antwort `warning: insufficient_data`

### Requirement: Tool `compare_periods`
`compare_periods(a, b, metrics, filters)` SHALL Δ, CI, n und Bewertung gemäß analytics-trends liefern.

#### Scenario: Frage-Workflow
- WHEN Claude nach `sync_source(trackman)` `compare_periods` mit neuen vs. vorherigen Runden aufruft
- THEN erhält Claude belastbare Δ-Werte mit Unsicherheit

### Requirement: Tool `get_club_gapping`
`get_club_gapping(filters)` SHALL Carry-Mediane, Lücken, Überlappungen, n je Schläger/Quelle liefern.

#### Scenario: Warnung
- WHEN Lücke > Schwelle
- THEN enthält die Antwort `gap_warning`

### Requirement: Tool `get_strokes_gained`
`get_strokes_gained(period|round_id, baseline?)` SHALL SG je Kategorie mit Baseline-ID, Abdeckung, n, CI liefern; für Range-Daten „nicht anwendbar".

#### Scenario: Teilabdeckung
- WHEN Abdeckung < Schwelle
- THEN wird keine Summe geliefert, sondern Abdeckung und Grund

### Requirement: Tool `get_insights`
`get_insights(source?, include_uncertain=false, limit?)` SHALL regelbasierte, deterministische Hinweise liefern, wo Schläge verloren gehen (größte SG-Verluste, belegte Veränderungen, Seiten-Bias, Gapping-Lücken). Jeder Hinweis SHALL Effekt, 95-%-Intervall, n, Quelle und einen Verweis auf ein Werkzeug mit Argumenten enthalten, mit dem die Zahlen nachprüfbar sind. Unsichere Hinweise (Intervall schließt 0 ein) SHALL standardmäßig ausgeblendet sein; Hinweise SHALL je Quelle getrennt erzeugt werden.

#### Scenario: Putting-Verlust
- WHEN SG Putten über mindestens 5 Runden im Mittel negativ ist und das Intervall die Null nicht einschließt
- THEN enthält die Antwort einen Hinweis mit Effekt pro 18 Löcher, Intervall und n

#### Scenario: Unsicher
- WHEN das Intervall den Nullwert einschließt
- THEN fehlt der Hinweis, außer `include_uncertain` ist gesetzt; dann ist er als `unsicher` markiert

### Requirement: Tool `add_note`
`add_note(text, type, date, scope)` SHALL eine datierte Notiz anlegen und die ID zurückgeben; Eingaben SHALL validiert werden.

#### Scenario: Ungültiges Datum
- WHEN das Datum nicht parsebar ist
- THEN wird ein Validierungsfehler geliefert und nichts gespeichert

### Requirement: Sicherheit
Der Server SHALL standardmäßig über stdio oder lokal gebundenes HTTP laufen; bei HTTP SHALL ein Token verlangt werden; Antworten SHALL keine Secrets und keine Rohdaten-Dateipfade außerhalb des Datenverzeichnisses enthalten. Daten aus Quellen sind untrusted und SHALL nicht als Anweisung interpretiert werden (z. B. Notiztext, Kursnamen).

#### Scenario: Remote-Zugriff ohne Token
- WHEN ein HTTP-Request ohne Token kommt
- THEN wird er mit 401 abgelehnt
