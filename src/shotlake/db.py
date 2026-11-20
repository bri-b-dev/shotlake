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
    par_played integer,
    tee_name varchar,
    tee_rating double,
    tee_slope integer,
    in_progress boolean not null,
    exclude_from_stats boolean not null,
    simulated boolean not null default false,
    content_hash varchar not null,
    raw_hash varchar not null,
    import_run_id varchar not null
);
create table if not exists round_input (
    round_id varchar primary key,
    handicap_relevant boolean,
    handicap_index_at_play double,
    pcc double,
    ags_override integer,
    note varchar,
    updated_at timestamp not null
);
create table if not exists tee_rating (
    course_id varchar not null,
    tee_name varchar not null,
    part varchar not null,  -- front9 | back9
    rating double not null,
    slope integer not null,
    primary key (course_id, tee_name, part)
);
create table if not exists club_map (
    source varchar not null,
    club_ref varchar not null,
    club_type varchar not null,
    primary key (source, club_ref)
);
create table if not exists note (
    note_id varchar primary key,
    note_date date not null,
    valid_to date,
    type varchar not null,
    text varchar not null,
    scope varchar not null,
    created_at timestamp not null
);
create table if not exists round_match (
    round_a varchar not null,
    round_b varchar not null,
    status varchar not null,  -- auto | proposed | confirmed | rejected
    confidence varchar not null,
    rule varchar not null,
    minutes_apart double,
    score_delta integer,
    hole_diff integer,
    decided_by varchar not null,  -- auto | user
    decided_at timestamp not null,
    primary key (round_a, round_b)
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
    source_sg double,
    source_sg_facet varchar,
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


class DatabaseVersionError(RuntimeError):
    """Die Datenbankdatei stammt von einer neueren DuckDB-Version als der installierten."""


def open_db(path: str | Path, *, ensure_schema: bool = True) -> duckdb.DuckDBPyConnection:
    """Datenbank öffnen; Schema und Migration nur bei `ensure_schema` (einmal je Prozess genügt)."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        con = duckdb.connect(str(path))
    except duckdb.IOException as exc:
        if "newer version" in str(exc) or "version number" in str(exc):
            raise DatabaseVersionError(
                f"{path} wurde mit einer neueren DuckDB-Version geschrieben als der installierten "
                f"({duckdb.__version__}). Shotlake nutzt DuckDB 1.x (2.x stürzt auf Prozessoren "
                "ohne AVX2 ab). Die Datei lässt sich mit der schreibenden Version ins ältere "
                'Format kopieren: uv run --no-project --with "duckdb==2.0.0" python '
                f"scripts/migrate_duckdb.py {path} --replace"
            ) from None
        raise
    if ensure_schema:
        con.execute(SCHEMA)
        _migrate(con)
    return con


def _migrate(con: duckdb.DuckDBPyConnection) -> None:
    """Ältere Datenbanken: Nutzerfeld `round.handicap_relevant` nach `round_input` verschieben."""
    cols = {r[0] for r in con.execute("describe round").fetchall()}
    if "handicap_relevant" in cols:
        con.execute(
            "insert or ignore into round_input (round_id, handicap_relevant, updated_at) "
            "select round_id, handicap_relevant, now()::timestamp from round "
            "where handicap_relevant is not null"
        )
        con.execute("alter table round drop column handicap_relevant")
    if "par_played" not in cols:
        con.execute("alter table round add column par_played integer")
    note_cols = {r[0] for r in con.execute("describe note").fetchall()}
    if "valid_to" not in note_cols:
        con.execute("alter table note add column valid_to date")
    shot_cols = {r[0] for r in con.execute("describe course_shot").fetchall()}
    if "source_sg" not in shot_cols:
        con.execute("alter table course_shot add column source_sg double")
        con.execute("alter table course_shot add column source_sg_facet varchar")
    if "source_handicap_index" in cols:  # Handicap-Angaben der Quelle werden nicht übernommen
        con.execute("alter table round drop column source_handicap_index")
    if "simulated" not in cols:
        con.execute("alter table round add column simulated boolean default false")
