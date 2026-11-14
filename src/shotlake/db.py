"""DuckDB-Schema (raw-Register, Importläufe, canonical: session, shot)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .model import ShotRecord

_SKIP = {"flags", "shot_time_utc"}


def _sql_type(annotation: str) -> str:
    if annotation.startswith("float"):
        return "DOUBLE"
    if annotation.startswith("bool"):
        return "BOOLEAN"
    return "VARCHAR"


SHOT_FIELDS = [f.name for f in fields(ShotRecord) if f.name not in _SKIP]
_SHOT_COLUMNS = ",\n    ".join(
    f"{f.name} {_sql_type(str(f.type))}" for f in fields(ShotRecord) if f.name not in _SKIP
)

SCHEMA = f"""
create table if not exists raw_object (
    source varchar not null,
    content_hash varchar not null,
    filename varchar not null,
    size_bytes bigint not null,
    stored_path varchar,
    imported_at timestamp not null,
    primary key (source, content_hash)
);
create table if not exists import_run (
    run_id varchar primary key,
    source varchar not null,
    filename varchar not null,
    status varchar not null,
    started_at timestamp not null,
    finished_at timestamp,
    sessions_new integer default 0,
    shots_new integer default 0,
    shots_changed integer default 0,
    shots_unchanged integer default 0,
    errors varchar
);
create table if not exists sync_state (
    source varchar primary key,
    cursor varchar,
    updated_at timestamp not null
);
create table if not exists session (
    session_id varchar primary key,
    source varchar not null,
    session_key varchar not null,
    kind varchar not null,
    start_utc timestamp not null,
    raw_hash varchar not null,
    import_run_id varchar not null
);
create table if not exists shot (
    shot_id varchar primary key,
    session_id varchar not null,
    shot_time_utc timestamp,
    {_SHOT_COLUMNS},
    flags varchar[] not null,
    content_hash varchar not null,
    raw_hash varchar not null,
    import_run_id varchar not null
);
create table if not exists course (
    course_id varchar primary key,
    source varchar not null,
    name varchar not null,
    country varchar,
    city varchar,
    lat double,
    lon double,
    par_total integer
);
create table if not exists course_tee (
    course_id varchar not null,
    tee_name varchar not null,
    handicap_type varchar,
    rating double,
    slope integer,
    primary key (course_id, tee_name, handicap_type)
);
create table if not exists round (
    round_id varchar primary key,
    source varchar not null,
    round_key varchar not null,
    course_id varchar not null,
    start_utc timestamp not null,
    end_utc timestamp,
    round_type varchar,
    holes_played integer not null,
    gross_strokes integer,
    tee_name varchar,
    tee_rating double,
    tee_slope integer,
    in_progress boolean not null,
    exclude_from_stats boolean not null,
    handicap_relevant boolean,
    content_hash varchar not null,
    raw_hash varchar not null,
    import_run_id varchar not null
);
create table if not exists hole (
    round_id varchar not null,
    hole_number integer not null,
    par integer,
    stroke_index integer,
    strokes integer,
    putts integer,
    penalties integer,
    fairway_outcome varchar,
    pin_lat double,
    pin_lon double,
    primary key (round_id, hole_number)
);
create table if not exists course_shot (
    shot_id varchar primary key,
    round_id varchar not null,
    hole_number integer not null,
    shot_order integer not null,
    shot_time_utc timestamp,
    club_raw varchar,
    club_type varchar,
    shot_type varchar,
    shot_source varchar,
    distance_m double,
    start_lat double,
    start_lon double,
    start_lie varchar,
    start_lie_source varchar,
    end_lat double,
    end_lon double,
    end_lie varchar,
    end_lie_source varchar,
    exclude_from_stats boolean not null,
    content_hash varchar not null,
    raw_hash varchar not null,
    import_run_id varchar not null
);
"""


def naive_utc(value: datetime | None) -> datetime | None:
    """Zeitstempel werden als UTC ohne Zeitzonenangabe gespeichert (naive Werte gelten als UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def open_db(path: str | Path) -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con
