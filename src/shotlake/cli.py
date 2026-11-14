"""Kommandozeile: Import, Schlägerstatistik, Gapping."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .analytics.distance import club_stats, gapping
from .db import open_db
from .ingest import PARSERS, import_file

M_TO_YD = 1 / 0.9144


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(p.glob("*.csv")) if p.is_dir() else [p])
    return out


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
    imp.add_argument("source", choices=sorted(PARSERS))
    imp.add_argument("paths", nargs="+")

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = Path(args.data_dir)
    con = open_db(data / "shotlake.duckdb")
    try:
        if args.cmd == "import":
            code = 0
            for f in _files(args.paths):
                rep = import_file(con, args.source, f, raw_dir=data / "raw")
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
