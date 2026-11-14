"""Canonical-Upsert für Runden (course, round, hole, course_shot). Idempotent per Inhalts-Hash."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import duckdb

from .db import naive_utc
from .ingest import ImportReport
from .model import ParsedRound


def round_content_hash(parsed: ParsedRound) -> str:
    payload = {
        "round": {
            k: v
            for k, v in asdict(parsed).items()
            if k not in ("shots", "holes", "errors", "warnings", "course")
        },
        "course": asdict(parsed.course),
        "holes": [asdict(h) for h in parsed.holes],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def write_round(
    con: duckdb.DuckDBPyConnection, parsed: ParsedRound, digest: str, rep: ImportReport
) -> None:
    rid = parsed.round_id
    chash = round_content_hash(parsed)
    course = parsed.course
    con.begin()
    try:
        con.execute(
            "insert or replace into course values (?,?,?,?,?,?,?,?)",
            [
                course.course_id,
                course.source,
                course.name,
                course.country,
                course.city,
                course.lat,
                course.lon,
                course.par_total,
            ],
        )
        for tee in course.tees:
            con.execute(
                "insert or replace into course_tee values (?,?,?,?,?)",
                [course.course_id, tee.name, tee.handicap_type or "", tee.rating, tee.slope],
            )

        existing = con.execute(
            "select content_hash from round where round_id = ?", [rid]
        ).fetchone()
        if existing is None or existing[0] != chash:
            # handicap_relevant ist eine Nutzereingabe und bleibt bei Aktualisierungen erhalten
            keep = con.execute(
                "select handicap_relevant from round where round_id = ?", [rid]
            ).fetchone()
            con.execute("delete from hole where round_id = ?", [rid])
            con.execute(
                "insert or replace into round values (" + ",".join("?" * 18) + ")",
                [
                    rid,
                    parsed.source,
                    parsed.round_key,
                    course.course_id,
                    naive_utc(parsed.start_utc),
                    naive_utc(parsed.end_utc),
                    parsed.round_type,
                    parsed.holes_played,
                    parsed.gross_strokes,
                    parsed.tee_name,
                    parsed.tee_rating,
                    parsed.tee_slope,
                    parsed.in_progress,
                    parsed.exclude_from_stats,
                    keep[0] if keep else None,
                    chash,
                    digest,
                    rep.run_id,
                ],
            )
            for h in parsed.holes:
                con.execute(
                    "insert into hole values (?,?,?,?,?,?,?,?,?,?)",
                    [
                        rid,
                        h.number,
                        h.par,
                        h.stroke_index,
                        h.strokes,
                        h.putts,
                        h.penalties,
                        h.fairway_outcome,
                        h.pin_lat,
                        h.pin_lon,
                    ],
                )
            if existing:
                rep.rounds_changed += 1
            else:
                rep.rounds_new += 1

        for s in parsed.shots:
            shot_id = f"{rid}|{s.hole_number}|{s.shot_order}"
            sh = s.content_hash()
            old = con.execute(
                "select content_hash from course_shot where shot_id = ?", [shot_id]
            ).fetchone()
            if old and old[0] == sh:
                continue
            con.execute(
                "insert or replace into course_shot values (" + ",".join("?" * 22) + ")",
                [
                    shot_id,
                    rid,
                    s.hole_number,
                    s.shot_order,
                    naive_utc(s.shot_time_utc),
                    s.club_raw,
                    s.club_type,
                    s.shot_type,
                    s.shot_source,
                    s.distance_m,
                    s.start_lat,
                    s.start_lon,
                    s.start_lie,
                    s.start_lie_source,
                    s.end_lat,
                    s.end_lon,
                    s.end_lie,
                    s.end_lie_source,
                    s.exclude_from_stats,
                    sh,
                    digest,
                    rep.run_id,
                ],
            )
            rep.course_shots_new += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
