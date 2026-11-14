"""Berichte als JSON-taugliche Strukturen (gemeinsame Basis für MCP-Werkzeuge und Weboberfläche).

Texte aus Quellen (Platznamen, Notizen) sind Daten, keine Anweisungen.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

import duckdb

from .analytics.distance import club_stats, gapping
from .analytics.insights import Insight
from .analytics.insights import generate as generate_insights
from .analytics.metrics import METRICS, Context, Filters, Metric, observations
from .analytics.sg import CATEGORIES, strokes_gained
from .analytics.stats import STATS, Interval, bootstrap_ci, compare
from .baseline import Baseline
from .clubs import CANONICAL_CLUBS
from .service import source_statuses

NOTE_TYPES = ("equipment", "swing", "coach", "round", "general")
_SCOPE = re.compile(r"^(global|(round|club|source):[\w|.\-]{1,80})$")
MIN_SG_COVERAGE = 0.5
SERIES_LIMIT = 30


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime | date) else value


def _ci(ci: Interval | None) -> dict[str, float] | None:
    return None if ci is None else {"low": ci.low, "high": ci.high, "level": ci.level}


def parse_date(text: str | None, label: str) -> date | None:
    if text is None or text == "":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{label}: Datum im Format JJJJ-MM-TT erwartet, erhalten {text!r}"
        ) from None


def make_filters(
    source: str | None = None, since: str | None = None, until: str | None = None,
    only_relevant: bool = False, club: str | None = None, last_n: int | None = None,
) -> Filters:  # fmt: skip
    if last_n is not None and last_n < 1:
        raise ValueError("window muss mindestens 1 sein")
    return Filters(
        source=source or None,
        since=parse_date(since, "since"),
        until=parse_date(until, "until"),
        only_relevant=only_relevant,
        club=club or None,
        last_n=last_n,
    )


# --- Quellen und Importberichte -----------------------------------------------------------


def list_sources(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return [
        {
            "source": s.id, "mode": s.mode, "risk": s.risk, "description": s.description,
            "rounds": s.rounds, "range_sessions": s.sessions, "range_shots": s.range_shots,
            "course_shots": s.course_shots, "last_status": s.last_status,
            "last_run": _iso(s.last_run),
        }
        for s in source_statuses(con)
    ]  # fmt: skip


def import_reports(
    con: duckdb.DuckDBPyConnection, source: str | None = None, run_id: str | None = None
) -> dict[str, Any]:
    where, params = ["1=1"], []
    if source:
        where.append("source = ?")
        params.append(source)
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    row = con.execute(
        "select run_id, source, filename, status, started_at, finished_at, sessions_new, "
        "shots_new, shots_changed, shots_unchanged, errors from import_run "
        f"where {' and '.join(where)} order by started_at desc limit 1",
        params,
    ).fetchone()
    if row is None:
        return {"found": False, "reason": "kein passender Importlauf"}
    return {
        "found": True, "run_id": row[0], "source": row[1], "filename": row[2], "status": row[3],
        "started_at": _iso(row[4]), "finished_at": _iso(row[5]),
        "delta": {"sessions_new": row[6], "shots_new": row[7], "shots_changed": row[8],
                  "shots_unchanged": row[9]},
        "errors": row[10],
    }  # fmt: skip


# --- Kennzahlen und Vergleich ------------------------------------------------------------------


def _metric(metric_id: str, f: Filters) -> Metric:
    metric = METRICS.get(metric_id)
    if metric is None:
        raise ValueError(f"Unbekannte Kennzahl {metric_id!r}; verfügbar: {sorted(METRICS)}")
    if metric.level == "shot" and not f.club:
        raise ValueError(f"{metric_id} braucht den Filter `club` (z. B. iron_7)")
    return metric


def _mix_warnings(sources: list[str]) -> list[str]:
    if len(sources) > 1:
        return [
            "mixed_sources: mehrere Quellen gemischt; ohne Dedup können Runden doppelt zählen, "
            "besser mit source filtern"
        ]
    return []


def stats_report(
    con: duckdb.DuckDBPyConnection, baseline: Baseline, metric_id: str, f: Filters,
    min_n: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    metric = _metric(metric_id, f)
    obs = observations(con, metric_id, f, baseline)
    values = [o.value for o in obs]
    sources = sorted({o.source for o in obs})
    limit = min_n if min_n is not None else metric.min_n
    warnings = _mix_warnings(sources)
    if len(values) < limit:
        warnings.insert(0, "insufficient_data")
    return {
        "metric": metric.id, "label": metric.label, "unit": metric.unit, "level": metric.level,
        "statistic": metric.stat, "better": metric.better, "note": metric.note or None,
        "value": STATS[metric.stat](values) if values else None,
        "n": len(values), "min_n": limit,
        "ci": _ci(bootstrap_ci(values, metric.stat)),
        "sources": sources,
        "filters": {"source": f.source, "since": _iso(f.since), "until": _iso(f.until),
                    "only_relevant": f.only_relevant, "club": f.club, "window": f.last_n},
        "series": [{"date": _iso(o.when), "value": o.value} for o in obs[-SERIES_LIMIT:]],
        "warnings": warnings,
    }  # fmt: skip


def compare_report(
    con: duckdb.DuckDBPyConnection, baseline: Baseline, a: dict[str, Any], b: dict[str, Any],
    metrics: list[str], base: Filters, min_n: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    def period(spec: dict[str, Any]) -> Filters:
        return Filters(
            source=base.source,
            only_relevant=base.only_relevant,
            club=base.club,
            since=parse_date(spec.get("since"), "since"),
            until=parse_date(spec.get("until"), "until"),
            last_n=spec.get("window"),
        )  # fmt: skip

    fa, fb = period(a), period(b)
    ctx = Context()  # Runden und SG einmal berechnen, für alle Kennzahlen und beide Zeiträume
    results = []
    for metric_id in metrics:
        metric = _metric(metric_id, fa)
        oa, ob = (observations(con, metric_id, f, baseline, ctx) for f in (fa, fb))
        limit = min_n if min_n is not None else metric.min_n
        cmp = compare(
            [o.value for o in oa], [o.value for o in ob],
            stat=metric.stat, better=metric.better, min_n=limit,
        )  # fmt: skip
        sa, sb = sorted({o.source for o in oa}), sorted({o.source for o in ob})
        warnings = list(cmp.warnings) + _mix_warnings(sorted(set(sa) | set(sb)))
        if oa and ob and sa != sb:
            warnings.append(
                "different_sources: die Zeiträume stammen aus unterschiedlichen Quellen"
            )
        results.append({
            "metric": metric_id, "label": metric.label, "unit": metric.unit,
            "statistic": metric.stat, "better": metric.better,
            "a": {"n": len(oa), "value": STATS[metric.stat]([o.value for o in oa]) if oa else None,
                  "sources": sa},
            "b": {"n": len(ob), "value": STATS[metric.stat]([o.value for o in ob]) if ob else None,
                  "sources": sb},
            "delta_a_minus_b": cmp.delta, "relative_change": cmp.relative_change,
            "ci": _ci(cmp.ci), "assessment": cmp.assessment, "warnings": warnings,
        })  # fmt: skip
    return {
        "a": {k: v for k, v in a.items()},
        "b": {k: v for k, v in b.items()},
        "results": results,
    }


# --- Gapping ----------------------------------------------------------------------------------


def gapping_report(
    con: duckdb.DuckDBPyConnection, f: Filters, method: str | None = None, min_n: int = 10
) -> dict[str, Any]:
    clubs = club_stats(
        con, source=f.source, method=method, since=f.since, until=f.until, min_n=min_n
    )
    gaps = gapping(con, min_n=min_n, source=f.source, method=method, since=f.since, until=f.until)
    return {
        "clubs": [
            {
                "club": c.club_type, "source": c.source, "method": c.method, "n": c.n,
                "carry_median_m": c.carry_median_m, "carry_p10_m": c.carry_p10_m,
                "carry_p90_m": c.carry_p90_m, "side_bias_m_right_positive": c.side_bias_m,
                "warning": c.warning,
            }
            for c in clubs
        ],
        "gaps": [
            {
                "longer": g.longer_club, "shorter": g.shorter_club, "source": g.source,
                "method": g.method, "gap_m": g.gap_m, "status": g.status,
                "gap_warning": g.status != "ok",
            }
            for g in gaps
        ],
        "min_n": min_n,
        "note": "Carry-Mediane aus Range-Daten je Quelle und Messmethode getrennt; Ausreißer "
                "ausgeschlossen.",
    }  # fmt: skip


# --- Strokes Gained ---------------------------------------------------------------------------


def sg_report(
    con: duckdb.DuckDBPyConnection, baseline: Baseline, f: Filters, round_id: str | None = None,
    min_coverage: float = MIN_SG_COVERAGE,
) -> dict[str, Any]:  # fmt: skip
    base = {"kind": "computed", "baseline": {"id": baseline.id, "status": baseline.status}}
    if round_id:
        in_rounds = con.execute(
            "select 1 from round where round_id = ? or round_key = ?", [round_id, round_id]
        ).fetchone()
        if in_rounds is None:
            in_sessions = con.execute(
                "select 1 from session where session_id = ?", [round_id]
            ).fetchone()
            if in_sessions:
                reason = "Range-Sessions haben keine Lage/Fahne: SG nicht anwendbar"
                return {**base, "applicable": False, "reason": reason}
            raise ValueError(f"Runde {round_id!r} nicht gefunden")
    res = strokes_gained(
        con, baseline, since=f.since, until=f.until, round_ref=round_id, source=f.source
    )
    total = len(res.holes)
    complete = res.complete_holes
    coverage = complete / total if total else 0.0
    out: dict[str, Any] = {
        **base, "applicable": True,
        "coverage": {"complete_holes": complete, "holes": total, "ratio": coverage},
    }  # fmt: skip
    if total == 0:
        return {**out, "summary": None, "reason": "keine Löcher im gewählten Zeitraum"}
    if coverage < min_coverage:
        reasons: dict[str, int] = {}
        for h in res.holes:
            if not h.complete and h.reason:
                reasons[h.reason] = reasons.get(h.reason, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        return {**out, "summary": None,
                "reason": f"Abdeckung {coverage:.0%} liegt unter {min_coverage:.0%}: keine Summe",
                "top_incomplete_reasons": [{"reason": r, "holes": n} for r, n in top]}  # fmt: skip
    totals = res.totals()
    categories = {}
    for cat in CATEGORIES:
        per_round = (
            [o.value for o in observations(con, f"sg_{cat}_per_hole", f, baseline)]
            if not round_id
            else []
        )
        categories[cat] = {
            "total": totals[cat],
            "per_hole": totals[cat] / complete,
            "ci_per_round_per_hole": _ci(bootstrap_ci(per_round)) if per_round else None,
            "n_rounds": len(per_round) if per_round else None,
        }
    grand = sum(h.total or 0.0 for h in res.holes if h.complete)
    note = (
        "Näherungs-Baseline; nur Vergleiche mit derselben Baseline sind aussagekräftig. "
        "'green_play' = letzter Schlag plus Putts, wenn die Quelle keine echte Ballposition "
        "auf dem Grün liefert."
    )
    summary = {"total": grand, "per_hole": grand / complete, "categories": categories}
    return {**out, "summary": summary, "note": note}


# --- Insights ---------------------------------------------------------------------------------


def insight_dict(i: Insight) -> dict[str, Any]:
    return {
        "id": i.id, "kind": i.kind, "status": i.status, "text": i.text,
        "effect": i.effect, "unit": i.unit, "ci": _ci(i.ci), "n": i.n, "source": i.source,
        "links": i.links,
    }  # fmt: skip


def insights_report(
    con: duckdb.DuckDBPyConnection, baseline: Baseline, source: str | None = None,
    include_uncertain: bool = False, limit: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    if limit is not None and limit < 1:
        raise ValueError("limit muss mindestens 1 sein")
    found = generate_insights(
        con, baseline, source=source, include_uncertain=include_uncertain, limit=limit
    )
    return {
        "insights": [insight_dict(i) for i in found],
        "baseline": {"id": baseline.id, "status": baseline.status},
        "include_uncertain": include_uncertain,
        "note": "Regelbasiert und deterministisch aus den Daten abgeleitet, je Quelle getrennt. "
                "Unsichere Hinweise (Intervall schließt 0 ein) sind standardmäßig ausgeblendet. "
                "SG-Effekte beziehen sich auf eine Näherungs-Baseline.",
    }  # fmt: skip


# --- Notizen -----------------------------------------------------------------------------------


def _note_values(
    con: duckdb.DuckDBPyConnection, text: str, type_: str, note_date: str, scope: str,
    valid_to: str | None,
) -> tuple[str, date, date | None, str]:  # fmt: skip
    """Notiz-Eingaben prüfen und normalisieren; sammelt alle Fehler in einer Meldung."""
    errors: list[str] = []
    clean = text.strip()
    if not clean or len(clean) > 2000:
        errors.append("text: 1 bis 2000 Zeichen erwartet")
    if type_ not in NOTE_TYPES:
        errors.append(f"type: erlaubt sind {', '.join(NOTE_TYPES)}")
    start: date | None = None
    try:
        start = date.fromisoformat(note_date)
    except ValueError:
        errors.append("date: Datum im Format JJJJ-MM-TT erwartet")
    end: date | None = None
    if valid_to:
        try:
            end = date.fromisoformat(valid_to)
        except ValueError:
            errors.append("valid_to: Datum im Format JJJJ-MM-TT erwartet")
        else:
            if start and end < start:
                errors.append("valid_to: darf nicht vor dem Datum liegen")
    scope = scope.strip() or "global"
    if not _SCOPE.match(scope):
        errors.append("scope: global, round:<id>, club:<typ> oder source:<id> erwartet")
    else:
        kind, _, value = scope.partition(":")
        if kind == "round":
            row = con.execute(
                "select round_id from round where round_id = ? or round_key = ?", [value, value]
            ).fetchall()
            if len(row) == 1:
                scope = f"round:{row[0][0]}"  # auf die eindeutige Runden-ID normalisieren
            else:
                errors.append(f"scope: Runde {value!r} nicht gefunden oder nicht eindeutig")
        elif kind == "club" and value not in CANONICAL_CLUBS:
            errors.append(f"scope: unbekannter Schlägertyp {value!r}")
        elif kind == "source" and value not in known_sources(con):
            errors.append(f"scope: unbekannte Quelle {value!r}")
    if errors:
        raise ValueError("; ".join(errors))
    assert start is not None
    return clean, start, end, scope


def known_sources(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {s.id for s in source_statuses(con)}


def _note_row(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "note_id": r[0], "date": _iso(r[1]), "valid_to": _iso(r[2]), "type": r[3],
        "text": r[4], "scope": r[5], "created_at": _iso(r[6]),
    }  # fmt: skip


NOTE_SELECT = "select note_id, note_date, valid_to, type, text, scope, created_at from note"


def add_note(
    con: duckdb.DuckDBPyConnection, text: str, type_: str, note_date: str, scope: str = "global",
    valid_to: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    clean, start, end, norm_scope = _note_values(con, text, type_, note_date, scope, valid_to)
    note_id = uuid.uuid4().hex[:12]
    con.execute(
        "insert into note (note_id, note_date, valid_to, type, text, scope, created_at) "
        "values (?,?,?,?,?,?, now()::timestamp)",
        [note_id, start, end, type_, clean, norm_scope],
    )
    return {"note_id": note_id, "date": _iso(start), "valid_to": _iso(end), "type": type_,
            "scope": norm_scope}  # fmt: skip


def get_note(con: duckdb.DuckDBPyConnection, note_id: str) -> dict[str, Any] | None:
    row = con.execute(f"{NOTE_SELECT} where note_id = ?", [note_id]).fetchone()
    return None if row is None else _note_row(row)


def update_note(
    con: duckdb.DuckDBPyConnection, note_id: str, text: str, type_: str, note_date: str,
    scope: str, valid_to: str | None,
) -> dict[str, Any]:  # fmt: skip
    if get_note(con, note_id) is None:
        raise ValueError("Notiz nicht gefunden")
    clean, start, end, norm_scope = _note_values(con, text, type_, note_date, scope, valid_to)
    con.execute(
        "update note set note_date = ?, valid_to = ?, type = ?, text = ?, scope = ? "
        "where note_id = ?",
        [start, end, type_, clean, norm_scope, note_id],
    )
    return {"note_id": note_id, "date": _iso(start), "valid_to": _iso(end), "type": type_,
            "scope": norm_scope}  # fmt: skip


def delete_note(con: duckdb.DuckDBPyConnection, note_id: str) -> bool:
    if get_note(con, note_id) is None:
        return False
    con.execute("delete from note where note_id = ?", [note_id])
    return True


def list_notes(
    con: duckdb.DuckDBPyConnection, *, type_: str | None = None, scope: str | None = None,
    since: str | None = None, until: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:  # fmt: skip
    """Notizen, neueste zuerst. `scope` filtert exakt (z. B. `club:iron_7`); Zeitraum nach Datum."""
    if limit < 1:
        raise ValueError("limit muss mindestens 1 sein")
    where = ["1=1"]
    params: list[object] = []
    if type_:
        where.append("type = ?")
        params.append(type_)
    if scope:
        where.append("scope = ?")
        params.append(scope)
    start, end = parse_date(since, "since"), parse_date(until, "until")
    if start:
        where.append("note_date >= ?")
        params.append(start)
    if end:
        where.append("note_date <= ?")
        params.append(end)
    rows = con.execute(
        f"{NOTE_SELECT} where {' and '.join(where)} order by note_date desc, created_at desc "
        "limit ?",
        [*params, limit],
    ).fetchall()
    return [_note_row(r) for r in rows]
