"""Kommandozeile: Import, Schlägerstatistik, Gapping."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import duckdb

from .analytics.distance import club_stats, gapping
from .analytics.score import PCC_MAX, PCC_MIN, RoundScore, round_scores
from .connectors.registry import API_CONNECTORS, CONNECTORS
from .db import open_db
from .ingest import sync_location
from .ingest_api import reprocess_raw, sync_api

M_TO_YD = 1 / 0.9144


def _fmt(value: float | None, factor: float, digits: int = 1) -> str:
    return "-" if value is None else f"{value * factor:.{digits}f}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="shotlake")
    ap.add_argument(
        "--data-dir",
        default=os.environ.get("SHOTLAKE_DATA_DIR", "data"),
        help="Verzeichnis für DB und raw-Dateien (Standard: ./data)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="Dateien einer Quelle importieren")
    imp.add_argument("source", choices=sorted(CONNECTORS))
    imp.add_argument("paths", nargs="+")

    syn = sub.add_parser("sync", help="API-Quelle inkrementell abrufen")
    syn.add_argument("source", choices=sorted(API_CONNECTORS))

    rep_p = sub.add_parser("reprocess", help="Canonical aus gespeicherten Rohdaten neu aufbauen")
    rep_p.add_argument("source", choices=sorted(API_CONNECTORS))

    rd = sub.add_parser("rounds", help="Runden mit Score, Statistik und Score Differential")
    rd.add_argument("--relevant", action="store_true", help="nur handicap-relevante Runden")

    rs = sub.add_parser("round-set", help="Eingaben zu einer Runde setzen")
    rs.add_argument("round", help="Runden-ID oder Schlüssel")
    rs.add_argument("--relevant", choices=["yes", "no", "auto"])
    rs.add_argument("--hi", type=float, help="Handicap-Index zum Spielzeitpunkt")
    rs.add_argument("--pcc", type=float, help=f"PCC ({PCC_MIN} bis +{PCC_MAX})")
    rs.add_argument("--ags", type=int, help="Adjusted Gross Score manuell")
    rs.add_argument("--clear", nargs="+", choices=["hi", "pcc", "ags"], default=[])

    ts = sub.add_parser("tee-set", help="9-Loch-Rating/Slope eines Tees hinterlegen")
    ts.add_argument("round", help="Runde, aus der Platz und Tee stammen")
    ts.add_argument("--part", choices=["front9", "back9"], required=True)
    ts.add_argument("--rating", type=float, required=True)
    ts.add_argument("--slope", type=int, required=True)

    sub.add_parser("sources", help="Verfügbare Quellen und Risiko anzeigen")

    for name in ("clubs", "gapping"):
        p = sub.add_parser(name)
        p.add_argument("--source")
        p.add_argument("--min-n", type=int, default=10)
        p.add_argument("--units", choices=["m", "yd"], default="m")
        p.add_argument("--include-flagged", action="store_true")
        p.add_argument("--since", type=date.fromisoformat, help="ab Datum (YYYY-MM-DD)")
        p.add_argument("--until", type=date.fromisoformat, help="bis einschließlich (YYYY-MM-DD)")
        where = p.add_mutually_exclusive_group()
        where.add_argument("--outdoor", dest="indoor", action="store_const", const=False)
        where.add_argument("--indoor", dest="indoor", action="store_const", const=True)
        p.set_defaults(indoor=None)
        if name == "clubs":
            p.add_argument("--club")
            p.add_argument("--combine", action="store_true")
    return ap


def _flag(r: RoundScore) -> str:
    if r.relevant is not None:
        return "ja*" if r.relevant else "nein*"
    return "ja?" if r.relevant_suggestion else "nein?"


def _pct(hits: int, recorded: int) -> str:
    rate = RoundScore.rate(hits, recorded)
    return "-" if rate is None else f"{rate:.0%}"


def _print_rounds(rows: list[RoundScore]) -> None:
    print(
        f"{'Datum':<11}{'Platz':<22}{'L':>3} {'Brutto':>6} {'+/-Par':>6} {'Putts':>5} {'FIR':>4}"
        f" {'GIR':>4} {'AGS':>4} {'Diff':>6} {'Rel':<6} Hinweis"
    )
    for r in rows:
        diff = "-" if r.differential is None else f"{r.differential:.1f}"
        to_par = "-" if r.to_par is None else f"{r.to_par:+d}"
        print(
            f"{r.start_utc:%Y-%m-%d}  {r.course_name[:20]:<22}{r.holes_played:>3} "
            f"{r.gross if r.gross is not None else '-':>6} {to_par:>6} "
            f"{r.putts if r.putts is not None else '-':>5} "
            f"{_pct(r.fir_hits, r.fir_recorded):>4} {_pct(r.gir_hits, r.gir_recorded):>4} "
            f"{r.ags if r.ags is not None else '-':>4} {diff:>6} {_flag(r):<6} {r.reason or ''}"
        )
    print("Rel: * = Nutzerentscheidung, ? = Vorschlag")


def _round_set(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    found = round_scores(con, round_ref=args.round)
    if len(found) != 1:
        print(f"Runde {args.round!r}: {len(found)} Treffer", file=sys.stderr)
        return 2
    if args.pcc is not None and not PCC_MIN <= args.pcc <= PCC_MAX:
        print(f"PCC muss zwischen {PCC_MIN} und {PCC_MAX} liegen", file=sys.stderr)
        return 2
    rid = found[0].round_id
    con.execute(
        "insert or ignore into round_input (round_id, updated_at) values (?, now()::timestamp)",
        [rid],
    )
    updates: dict[str, object] = {}
    if args.relevant:
        updates["handicap_relevant"] = {"yes": True, "no": False, "auto": None}[args.relevant]
    if args.hi is not None:
        updates["handicap_index_at_play"] = args.hi
    if args.pcc is not None:
        updates["pcc"] = args.pcc
    if args.ags is not None:
        updates["ags_override"] = args.ags
    for key in args.clear:
        updates[{"hi": "handicap_index_at_play", "pcc": "pcc", "ags": "ags_override"}[key]] = None
    for column, value in updates.items():
        con.execute(f"update round_input set {column} = ? where round_id = ?", [value, rid])
    con.execute("update round_input set updated_at = now()::timestamp where round_id = ?", [rid])
    _print_rounds(round_scores(con, round_ref=rid))
    return 0


def _tee_set(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    row = con.execute(
        "select course_id, tee_name from round where round_id = ? or round_key = ?",
        [args.round, args.round],
    ).fetchall()
    if len(row) != 1 or row[0][1] is None:
        print(f"Runde {args.round!r} nicht eindeutig oder ohne Tee", file=sys.stderr)
        return 2
    course_id, tee = row[0]
    con.execute(
        "insert or replace into tee_rating values (?,?,?,?,?)",
        [course_id, tee, args.part, args.rating, args.slope],
    )
    print(f"{course_id} / {tee} / {args.part}: Rating {args.rating}, Slope {args.slope}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = Path(args.data_dir)
    con = open_db(data / "shotlake.duckdb")
    try:
        if args.cmd == "sources":
            for c in CONNECTORS.values():
                print(f"{c.id:<16}{c.mode:<6}{c.risk:<22}{c.description}")
            for factory in API_CONNECTORS.values():
                a = factory()
                print(f"{a.id:<16}{a.mode:<6}{a.risk:<22}{a.description}")
            return 0
        if args.cmd == "rounds":
            _print_rounds(round_scores(con, only_relevant=args.relevant))
            return 0
        if args.cmd == "round-set":
            return _round_set(con, args)
        if args.cmd == "tee-set":
            return _tee_set(con, args)
        if args.cmd == "reprocess":
            rep = reprocess_raw(con, API_CONNECTORS[args.source]())
            print(rep.as_text())
            return 0 if rep.status == "ok" else 2
        if args.cmd == "sync":
            rep = sync_api(con, API_CONNECTORS[args.source](), raw_dir=data / "raw")
            print(rep.as_text())
            return 0 if rep.status == "ok" else 2
        if args.cmd == "import":
            code = 0
            for path in args.paths:
                for rep in sync_location(con, args.source, path, raw_dir=data / "raw"):
                    print(rep.as_text())
                    if rep.status in ("drift_detected", "error"):
                        code = 2
            return code

        factor, unit = (M_TO_YD, "yd") if args.units == "yd" else (1.0, "m")
        if args.cmd == "clubs":
            rows = club_stats(
                con,
                club_type=args.club,
                source=args.source,
                min_n=args.min_n,
                include_flagged=args.include_flagged,
                combine=args.combine,
                since=args.since,
                until=args.until,
                indoor=args.indoor,
            )
            print(
                f"{'Schläger':<15}{'Quelle':<14}{'n':>4} {'Carry-Median':>13}"
                f" {'P10':>6} {'P90':>6} {'Bias':>7} {'Streu':>6}  Hinweis"
            )
            for s in rows:
                note = s.warning or ("gemischt" if s.mixed else "")
                print(
                    f"{s.club_type:<15}{s.source:<14}{s.n:>4} "
                    f"{_fmt(s.carry_median_m, factor):>10} {unit:<2}"
                    f"{_fmt(s.carry_p10_m, factor):>6} {_fmt(s.carry_p90_m, factor):>6} "
                    f"{_fmt(s.side_bias_m, factor):>7} {_fmt(s.side_std_m, factor):>6}  {note}"
                )
            return 0

        gaps = gapping(
            con,
            min_n=args.min_n,
            source=args.source,
            include_flagged=args.include_flagged,
            since=args.since,
            until=args.until,
            indoor=args.indoor,
        )
        for g in gaps:
            print(
                f"{g.source:<14}{g.longer_club:<15}-> {g.shorter_club:<15}"
                f"Lücke {_fmt(g.gap_m, factor)} {unit}  {g.status}"
            )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
