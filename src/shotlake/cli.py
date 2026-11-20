"""Kommandozeile: Import, Schlägerstatistik, Gapping."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path

import duckdb

from .analytics.course_shots import (
    approach_green_rate,
    club_distances,
    start_lies,
    tee_shot_outcomes,
)
from .analytics.distance import DEFAULT_MIN_SPEED_RATIO, club_stats, gapping
from .analytics.insights import generate as generate_insights
from .analytics.score import PCC_MAX, PCC_MIN, RoundScore, round_scores
from .analytics.sg import CATEGORIES, strokes_gained
from .analytics.trend import differential_trend
from .backup import (
    BackupError,
    create_backup,
    default_archive_name,
    export_key,
    inspect_backup,
    is_encrypted,
    restore_backup,
)
from .baseline import default_baseline_path, list_baselines, load_baseline, resolve_baseline_path
from .clubs import CANONICAL_CLUBS
from .config import default_club_map_path, load_club_map, save_club_map, sync_club_map
from .connectors.arccos import ArccosConnector, HarArccosClient
from .connectors.registry import API_CONNECTORS, CONNECTORS
from .db import DatabaseVersionError, open_db
from .dedup import Scope, default_dedup_config_path, load_config
from .export import FORMATS, export_all, export_zip
from .ingest import sync_location
from .ingest_api import reprocess_raw, sync_api
from .secret_store import use_store

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
    ap.add_argument(
        "--club-map",
        type=Path,
        default=default_club_map_path(),
        help="Schlägerzuordnung (TOML, Standard: config/club_map.toml oder SHOTLAKE_CLUB_MAP)",
    )
    ap.add_argument(
        "--dedup-config",
        type=Path,
        default=default_dedup_config_path(),
        help="Dedup-Regeln (TOML, Standard: config/dedup.toml oder SHOTLAKE_DEDUP_CONFIG)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="Dateien einer Quelle importieren")
    imp.add_argument("source", choices=sorted(CONNECTORS))
    imp.add_argument("paths", nargs="+")

    syn = sub.add_parser("sync", help="API-Quelle inkrementell abrufen")
    syn.add_argument("source", choices=sorted(API_CONNECTORS))
    syn.add_argument("--har", type=Path, help="Arccos: Browser-HAR-Export des Dashboards einlesen")

    rep_p = sub.add_parser("reprocess", help="Canonical aus gespeicherten Rohdaten neu aufbauen")
    rep_p.add_argument("source", choices=sorted(API_CONNECTORS))

    rd = sub.add_parser("rounds", help="Runden mit Score, Statistik und Score Differential")
    rd.add_argument("--relevant", action="store_true", help="nur handicap-relevante Runden")
    rd.add_argument("--source", help="nur eine Quelle")
    rd.add_argument(
        "--include-duplicates", action="store_true", help="auch nicht führende Dubletten"
    )

    cc = sub.add_parser("course-shots", help="Schläge auf dem Platz: Distanzen, Lagen, Treffer")
    cc.add_argument("--round", dest="round_ref")
    cc.add_argument("--source", help="nur eine Quelle")
    cc.add_argument(
        "--include-duplicates", action="store_true", help="auch nicht führende Dubletten"
    )
    cc.add_argument("--since", type=date.fromisoformat)
    cc.add_argument("--until", type=date.fromisoformat)
    cc.add_argument("--min-n", type=int, default=5)
    cc.add_argument("--units", choices=["m", "yd"], default="m")

    sg = sub.add_parser("sg", help="Strokes Gained (selbst berechnet, Handicap-Band-Baseline)")
    sg.add_argument("--round", dest="round_ref")
    sg.add_argument("--source", help="nur eine Quelle")
    sg.add_argument(
        "--include-duplicates", action="store_true", help="auch nicht führende Dubletten"
    )
    sg.add_argument("--since", type=date.fromisoformat)
    sg.add_argument("--until", type=date.fromisoformat)
    sg.add_argument(
        "--baseline",
        type=Path,
        default=default_baseline_path(),
        help="Baseline-Datei oder Kennung (siehe `baselines`); Standard: eigenes Niveau",
    )
    sg.add_argument("--reasons", action="store_true", help="Gründe unvollständiger Löcher zeigen")

    cs = sub.add_parser("club-set", help="Quell-Schläger einem Schlägertyp zuordnen")
    cs.add_argument("source")
    cs.add_argument("club_ref", help="z. B. garmin_clubtype_1")
    cs.add_argument("club_type", choices=CANONICAL_CLUBS)

    tr = sub.add_parser("trend", help="Verlauf des Score Differentials (gleitender Durchschnitt)")
    tr.add_argument("--window", type=int, default=5, help="Fenstergröße in Runden (Standard 5)")
    tr.add_argument("--relevant", action="store_true", help="nur handicap-relevante Runden")
    tr.add_argument("--source", help="nur eine Quelle")
    tr.add_argument(
        "--include-duplicates", action="store_true", help="auch nicht führende Dubletten"
    )

    rs = sub.add_parser("round-set", help="Eingaben zu einer Runde setzen")
    rs.add_argument("round", help="Runden-ID oder Schlüssel")
    rs.add_argument("--relevant", choices=["yes", "no", "auto"], help="auto = zurück auf nein")
    rs.add_argument("--hi", type=float, help="Handicap-Index zum Spielzeitpunkt")
    rs.add_argument("--pcc", type=float, help=f"PCC ({PCC_MIN} bis +{PCC_MAX})")
    rs.add_argument("--ags", type=int, help="AGS (bereinigtes Brutto) manuell")
    rs.add_argument("--clear", nargs="+", choices=["hi", "pcc", "ags"], default=[])

    ts = sub.add_parser(
        "tee-set", help="Rating/Slope eines Tees hinterlegen (all = 18 Löcher, sonst 9 Löcher)"
    )
    ts.add_argument("round", help="Runde, aus der Platz und Tee stammen")
    ts.add_argument("--part", choices=["all", "front9", "back9"], required=True)
    ts.add_argument("--rating", type=float, required=True)
    ts.add_argument("--slope", type=int, required=True)

    sub.add_parser(
        "arccos-check", help="Arccos-Live-Zugang prüfen (nur Zählwerte, nichts speichern)"
    )
    sv = sub.add_parser("serve", help="Weboberfläche starten (Passwort beim ersten Aufruf)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--baseline-path", type=Path, default=default_baseline_path())
    mc = sub.add_parser("mcp", help="MCP-Server starten (stdio oder lokal gebundenes HTTP)")
    mc.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    mc.add_argument(
        "--read-only", action="store_true", help="sync_source und add_note nicht anbieten"
    )
    mc.add_argument("--host", default="127.0.0.1")
    mc.add_argument("--port", type=int, default=8765)
    mc.add_argument("--token", default=os.environ.get("SHOTLAKE_MCP_TOKEN", ""))
    mc.add_argument("--baseline-path", type=Path, default=default_baseline_path())
    ins = sub.add_parser("insights", help="Regelbasierte Insights, wo Schläge verloren gehen")
    ins.add_argument("--source")
    ins.add_argument(
        "--include-duplicates", action="store_true", help="je Quelle statt dedupliziert"
    )
    ins.add_argument("--all", action="store_true", help="auch unsichere Insights zeigen")
    ins.add_argument("--baseline-path", type=Path, default=default_baseline_path())
    dd = sub.add_parser("dedup", help="Runden aus mehreren Quellen abgleichen und verknüpfen")
    dds = dd.add_subparsers(dest="dedup_cmd")
    dds.add_parser("run", help="Treffer neu berechnen (Standard) und Vorschläge anzeigen")
    dds.add_parser("list", help="verknüpfte Runden und offene Vorschläge anzeigen")
    for name, text in (
        ("confirm", "zwei Runden als dieselbe Runde bestätigen oder zusammenführen"),
        ("reject", "zwei Runden als verschieden festlegen (trennt bestehende Verknüpfung)"),
    ):
        p = dds.add_parser(name, help=text)
        p.add_argument("round_a")
        p.add_argument("round_b")
    dds.add_parser("accept-all", help="alle offenen Vorschläge bestätigen")
    bk = sub.add_parser("backup", help="Sicherung des Datenverzeichnisses schreiben")
    bk.add_argument("--out", type=Path, help="Zieldatei (Standard: <data-dir>/backups/…)")
    bk.add_argument(
        "--password",
        action="store_true",
        help="Archiv verschlüsseln (Passwort aus SHOTLAKE_BACKUP_PASSWORD oder Abfrage)",
    )
    rs = sub.add_parser("restore", help="Sicherung einspielen (ersetzt Datenbank und Rohdaten)")
    rs.add_argument("archive", type=Path)
    rs.add_argument("--yes", action="store_true", help="ohne Rückfrage")
    vf = sub.add_parser("backup-verify", help="Sicherung prüfen, ohne etwas zu ändern")
    vf.add_argument("archive", type=Path)
    ex = sub.add_parser("export", help="Alle Tabellen als Parquet und CSV exportieren")
    ex.add_argument(
        "--out", type=Path, help="Zielverzeichnis (Standard: <data-dir>/exports/<Zeit>)"
    )
    ex.add_argument("--format", choices=[*FORMATS, "all"], default="all")
    ex.add_argument("--zip", action="store_true", help="als ZIP-Datei statt Verzeichnis schreiben")
    bkey = sub.add_parser("backup-key", help="Schlüssel der Zugangsdaten zum getrennten Sichern")
    bkey.add_argument("--out", type=Path, help="in eine Datei schreiben (Rechte 0600)")
    sub.add_parser("baselines", help="Verfügbare Strokes-Gained-Baselines anzeigen")
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
        p.add_argument(
            "--all-swings",
            action="store_true",
            help="auch Teilschläge/Chips (Ballgeschwindigkeit < 70 %% des Schläger-Medians)",
        )
        if name == "clubs":
            p.add_argument("--club")
            p.add_argument("--combine", action="store_true")
    return ap


def _flag(r: RoundScore) -> str:
    return "ja" if r.relevant_effective else "nein"


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
        diff += "x" if r.extrapolated else ""
        to_par = "-" if r.to_par is None else f"{r.to_par:+d}"
        print(
            f"{r.start_utc:%Y-%m-%d}  {r.course_name[:20]:<22}{r.holes_played:>3} "
            f"{r.gross if r.gross is not None else '-':>6} {to_par:>6} "
            f"{r.putts if r.putts is not None else '-':>5} "
            f"{_pct(r.fir_hits, r.fir_recorded):>4} {_pct(r.gir_hits, r.gir_recorded):>4} "
            f"{r.ags if r.ags is not None else '-':>4} {diff:>6} {_flag(r):<6} {r.reason or ''}"
        )
    print("Rel: ja nur, wenn du es gesetzt hast (Standard nein).")
    print("Diff x = 9 Löcher auf 18 hochgerechnet (WHS mit Index, sonst verdoppelt).")


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
    if len(row) != 1:
        print(f"Runde {args.round!r} nicht eindeutig", file=sys.stderr)
        return 2
    course_id, tee = row[0][0], row[0][1] or ""  # Quellen ohne Tee-Angabe: Tee "" (unbekannt)
    con.execute(
        "insert or replace into tee_rating values (?,?,?,?,?)",
        [course_id, tee, args.part, args.rating, args.slope],
    )
    print(f"{course_id} / {tee} / {args.part}: Rating {args.rating}, Slope {args.slope}")
    return 0


def _dedup_command(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> int:
    from . import dedup as dd

    action = args.dedup_cmd or "run"
    try:
        if action in ("confirm", "reject"):
            a, b = dd.resolve(con, args.round_a), dd.resolve(con, args.round_b)
            (dd.confirm if action == "confirm" else dd.reject)(con, a, b)
            print(f"{a} / {b}: {'bestätigt' if action == 'confirm' else 'getrennt (abgelehnt)'}")
            return 0
        if action == "accept-all":
            done, skipped = dd.accept_proposed(con)
            print(f"{done} Vorschläge bestätigt")
            for line in skipped:
                print(f"  übersprungen: {line}")
            return 0
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    return _print_dedup(con, args, run_first=action == "run")


def _arccos_check(cache_dir: Path) -> int:
    """Kontrollierter Erstkontakt: Login, Hash-Suche, Jahre, Rundenzahl. Speichert nichts."""
    from .connectors.api import AuthError, TransientError
    from .connectors.errors import SchemaDriftError

    connector = ArccosConnector(cache_dir=cache_dir)
    steps = [("Login und Funktions-Hashes", connector.authenticate)]
    try:
        for label, step in steps:
            step()
            print(f"OK   {label}")
        client = connector.client
        assert client is not None
        rows = client.rounds()
        ended = sum(1 for r in rows if r.get("ended"))
        years = sorted({str(r.get("startTime"))[:4] for r in rows})
        print(f"OK   Rundenliste: {len(rows)} Runden ({ended} beendet), Jahre {', '.join(years)}")
        if rows:
            detail = client.round_detail(int(rows[0]["roundId"]))
            shots = sum(
                len(h.get("shots", [])) for h in (detail or {}).get("round", {}).get("holes", [])
            )
            print(f"OK   Detail der neuesten Runde: {shots} Schläge")
    except (AuthError, TransientError, SchemaDriftError) as exc:
        print(f"FEHLER {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


def _print_sg(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> None:
    base = load_baseline(resolve_baseline_path(args.baseline))
    res = strokes_gained(
        con,
        base,
        since=args.since,
        until=args.until,
        round_ref=args.round_ref,
        source=args.source,
        exclude_rounds=_excluded(_scope(con, args), args, "shots"),
    )
    n_all = len(res.holes)
    print(f"Strokes Gained (computed), Baseline {base.id} [{base.status}]")
    print(f"Vollständig erfasste Löcher: {res.complete_holes} von {n_all}")
    if not res.complete_holes:
        print("Keine vollständig erfassten Löcher, keine SG-Werte.")
    else:
        names = {
            "tee": "Abschlag", "approach": "Annäherung", "short": "Kurzes Spiel",
            "green_play": "Aufs Grün+Putts",
            "putting": "Putten", "penalty": "Strafschläge",
        }  # fmt: skip
        totals = res.totals()
        total = sum(h.total or 0.0 for h in res.holes if h.complete)
        for cat in CATEGORIES:
            per_hole = totals[cat] / res.complete_holes
            print(f"  {names[cat]:<17}{totals[cat]:>+8.2f}  ({per_hole:+.2f}/Loch)")
        print(f"  {'Gesamt':<17}{total:>+8.2f}  ({total / res.complete_holes:+.2f}/Loch)")
    print("Hinweis: Summen nur über vollständig erfasste Löcher; Baseline ist eine Näherung.")
    print(
        "'Aufs Grün+Putts': letzter Schlag und Putts, nicht trennbar, wenn die Quelle keine echte "
        "Ballposition auf dem Grün liefert."
    )
    if args.reasons:
        from collections import Counter

        for reason, n in Counter(h.reason for h in res.holes if not h.complete).most_common():
            print(f"  unvollständig: {n}x {reason}")


def _print_course_shots(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> None:
    f = M_TO_YD if args.units == "yd" else 1.0
    kw = {
        "since": args.since,
        "until": args.until,
        "round_ref": args.round_ref,
        "source": args.source,
        "exclude_rounds": _excluded(_scope(con, args), args, "shots"),
    }
    print(f"Schlagdistanz je Schläger ({args.units}, GPS Start-Ende inkl. Rollen, kein Carry)")
    print(
        f"{'Schläger':<22}{'n':>4} {'P10':>6} {'Q1':>6} {'Median':>7} {'Q3':>6} {'P90':>6} "
        f"{'Max':>6}  Hinweis"
    )
    for c in club_distances(con, min_n=args.min_n, **kw):
        note = c.warning or ("" if c.mapped else "nicht zugeordnet (club-set)")
        print(
            f"{c.club:<22}{c.n:>4} {_fmt(c.p10_m, f):>6} {_fmt(c.p25_m, f):>6} "
            f"{_fmt(c.median_m, f):>7} {_fmt(c.p75_m, f):>6} {_fmt(c.p90_m, f):>6} "
            f"{_fmt(c.max_m, f):>6}  {note}"
        )
    print("\nAbschläge: Landelage")
    for t in tee_shot_outcomes(con, **kw):
        print(f"  {t.lie or 'unbekannt':<10}{t.n:>4}  {t.share:.0%}")
    print("\nSchläge nach Ausgangslage (ohne Putts)")
    for lie in start_lies(con, **kw):
        print(f"  {lie.lie or 'unbekannt':<10}{lie.n:>4}  {lie.share:.0%}")
    print("\nGrüntreffer nach Annäherung, je Ausgangslage")
    for lie_name, (hits, n) in sorted(approach_green_rate(con, **kw).items()):
        print(f"  {lie_name:<10}{hits:>3}/{n:<3} {hits / n:.0%}")


def _scope(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> Scope | None:
    """Dedup-Sicht; `--include-duplicates` schaltet sie ab."""
    if getattr(args, "include_duplicates", False):
        return None
    return Scope.build(con, load_config(args.dedup_config))


def _excluded(scope: Scope | None, args: argparse.Namespace, group: str) -> frozenset[str] | None:
    """Nicht führende Runden einer Feldgruppe, nur ohne Quellenfilter."""
    if scope is None or getattr(args, "source", None):
        return None
    return scope.exclude(group)


def _print_dedup(con: duckdb.DuckDBPyConnection, args: argparse.Namespace, run_first: bool) -> int:
    from . import dedup as dd

    cfg = load_config(args.dedup_config)
    if run_first:
        print(dd.run(con, cfg).as_text())
    groups = dd.cluster_facts(con)
    print(f"\nVerknüpfte Runden: {len(groups)}")
    for g in groups:
        lead = dd.leaders_of(g, cfg)
        parts = ", ".join(f"{f.source} ({f.gross if f.gross is not None else '-'})" for f in g)
        lead_text = f"führend: Score {lead['score']}, Schläge {lead['shots']}"
        print(f"  {g[0].start:%Y-%m-%d %H:%M}  {g[0].holes:>2} L  {parts}  {lead_text}")
    proposals = dd.list_matches(con, ("proposed",))
    print(f"\nOffene Vorschläge: {len(proposals)}")
    for m in proposals:
        print(
            f"  {m.a.start:%Y-%m-%d %H:%M}  {m.a.round_id} ({m.a.holes}L, {m.a.gross}) <-> "
            f"{m.b.round_id} ({m.b.holes}L, {m.b.gross})  {m.confidence}: {m.rule}, "
            f"{m.minutes_apart:.0f} min"
        )
    if proposals:
        print("\nBestätigen: shotlake dedup confirm A B | alle: shotlake dedup accept-all")
    return 0


def _auto_dedup(con: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> None:
    from . import dedup as dd

    print(dd.run(con, load_config(args.dedup_config)).as_text())


def _backup_password(encrypted: bool, ask_twice: bool) -> str | None:
    """Passwort aus der Umgebung oder per Abfrage; nie als Argument (History)."""
    if not encrypted:
        return None
    value = os.environ.get("SHOTLAKE_BACKUP_PASSWORD")
    if value:
        return value
    value = getpass.getpass("Passwort für das Archiv: ")
    if ask_twice and getpass.getpass("Passwort wiederholen: ") != value:
        raise BackupError("Die Passwörter stimmen nicht überein")
    if not value:
        raise BackupError("Leeres Passwort")
    return value


def _export_command(args: argparse.Namespace, data: Path) -> int:
    formats = FORMATS if args.format == "all" else (args.format,)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        if args.zip:
            target = args.out or data / "exports" / f"shotlake-export-{stamp}.zip"
            result = export_zip(data / "shotlake.duckdb", target, formats=formats)
        else:
            target = args.out or data / "exports" / f"export-{stamp}"
            result = export_all(data / "shotlake.duckdb", target, formats=formats)
    except (FileNotFoundError, FileExistsError, ValueError, duckdb.Error) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    rows = sum(result.tables.values())
    print(
        f"Export: {target} ({len(result.tables)} Tabellen, {rows} Zeilen, {result.files} Dateien)"
    )
    print(
        "  Beschreibung und Prüfsummen: schema.json; enthält keine Zugangsdaten, aber GPS-Rohwerte."
    )
    return 0


def _backup_command(args: argparse.Namespace, data: Path) -> int:
    """Sichern und Wiederherstellen brauchen die Datenbank ungeöffnet, deshalb vor `open_db`."""
    try:
        if args.cmd == "backup":
            password = _backup_password(args.password, ask_twice=True)
            target = args.out or data / "backups" / default_archive_name(password is not None)
            manifest = create_backup(data, target, password=password)
            rows = sum(manifest.tables.values())
            print(f"Sicherung: {target} ({target.stat().st_size / 1024**2:.1f} MB)")
            print(
                f"  {len(manifest.files)} Dateien, {rows} Zeilen in {len(manifest.tables)} Tabellen"
            )
            print("  Nicht enthalten: Schlüssel, Passwort der Oberfläche, Garmin-Token.")
            print("  Lege eine Kopie an einem anderen Ort ab (`backup-key` sichert den Schlüssel).")
            return 0
        if args.cmd == "backup-verify":
            password = _backup_password(is_encrypted(args.archive), ask_twice=False)
            manifest = inspect_backup(args.archive, password)
            print(
                f"In Ordnung: erstellt {manifest.created}, {len(manifest.files)} Dateien, "
                f"{sum(manifest.tables.values())} Zeilen, Format {manifest.format}"
            )
            return 0
        if args.cmd == "backup-key":
            key = export_key(data)
            if args.out:
                if args.out.exists():
                    raise BackupError(f"{args.out} existiert schon")
                fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(key + "\n")
                print(f"Schlüssel geschrieben nach {args.out} (Rechte 0600)")
            else:
                print(key)
            return 0
        # restore
        password = _backup_password(is_encrypted(args.archive), ask_twice=False)
        manifest = inspect_backup(args.archive, password)
        print(f"Sicherung vom {manifest.created}: {sum(manifest.tables.values())} Zeilen.")
        if not args.yes:
            answer = input("Datenbank, Rohdaten und Uploads in diesem Verzeichnis ersetzen? [j/N] ")
            if answer.strip().lower() not in ("j", "ja", "y", "yes"):
                print("Abgebrochen, nichts geändert.")
                return 1
        report = restore_backup(data, args.archive, password=password)
        print(f"Wiederhergestellt: {report.restored['rows']} Zeilen.")
        if report.safety_backup:
            print(f"Vorheriger Stand gesichert unter {report.safety_backup}")
        for note in report.notes:
            print(f"Hinweis: {note}")
        return 0
    except BackupError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = Path(args.data_dir)
    if args.cmd in ("backup", "restore", "backup-verify", "backup-key"):
        return _backup_command(args, data)
    if args.cmd == "export":
        return _export_command(args, data)
    if args.cmd == "serve":  # Fortschritt sofort sichtbar machen (Container-Log), auch bei Hängern
        print(
            f"Shotlake startet (DuckDB {duckdb.__version__}), Datenverzeichnis {data}", flush=True
        )
    use_store(data)  # im Frontend gepflegte Zugangsdaten für Syncs bereitstellen
    try:
        con = open_db(data / "shotlake.duckdb")
    except DatabaseVersionError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    try:
        sync_club_map(con, args.club_map)
        if args.cmd == "baselines":
            default = load_baseline(resolve_baseline_path(default_baseline_path()))
            for b in list_baselines(default_baseline_path().parent):
                mark = "Standard" if b.id == default.id else ""
                print(f"{b.id:<20}{b.title:<28}{b.status:<14}{mark}")
            return 0
        if args.cmd == "sources":
            for c in CONNECTORS.values():
                print(f"{c.id:<16}{c.mode:<6}{c.risk:<22}{c.description}")
            for factory in API_CONNECTORS.values():
                a = factory()
                print(f"{a.id:<16}{a.mode:<6}{a.risk:<22}{a.description}")
            return 0
        if args.cmd == "rounds":
            scope = _scope(con, args)
            _print_rounds(
                round_scores(
                    con,
                    only_relevant=args.relevant,
                    source=args.source,
                    exclude=_excluded(scope, args, "score"),
                    donors=scope.donors if scope else None,
                )
            )
            return 0
        if args.cmd == "club-set":
            mapping = load_club_map(args.club_map)
            mapping.setdefault(args.source, {})[args.club_ref] = args.club_type
            save_club_map(args.club_map, mapping)
            sync_club_map(con, args.club_map)
            print(f"{args.source}/{args.club_ref} -> {args.club_type}  ({args.club_map})")
            return 0
        if args.cmd == "sg":
            _print_sg(con, args)
            return 0
        if args.cmd == "course-shots":
            _print_course_shots(con, args)
            return 0
        if args.cmd == "trend":
            scope = _scope(con, args)
            scores = round_scores(
                con,
                source=args.source,
                exclude=_excluded(scope, args, "score"),
                donors=scope.donors if scope else None,
            )
            points = differential_trend(scores, args.window, args.relevant)
            print(f"{'Datum':<11}{'Diff':>6} {'Ø' + str(args.window):>6}  Rel")
            for p in points:
                avg = "-" if p.moving_avg is None else f"{p.moving_avg:.1f}"
                mark = "ja" if p.relevant else ""
                print(f"{p.start_utc:%Y-%m-%d} {p.differential:>6.1f} {avg:>6}  {mark}")
            return 0
        if args.cmd == "dedup":
            return _dedup_command(con, args)
        if args.cmd == "round-set":
            return _round_set(con, args)
        if args.cmd == "tee-set":
            return _tee_set(con, args)
        if args.cmd == "mcp":
            con.close()
            from .mcp_server import create_server, http_app
            from .workspace import Workspace

            server = create_server(
                Workspace(data, args.club_map, resolve_baseline_path(args.baseline_path)),
                read_only=args.read_only,
            )
            if args.transport == "stdio":
                server.run("stdio")  # stdout gehört dem Protokoll: hier nichts ausgeben
                return 0
            if not args.token:
                print("HTTP braucht ein Token (--token oder SHOTLAKE_MCP_TOKEN)", file=sys.stderr)
                return 2
            import uvicorn

            uvicorn.run(http_app(server, args.token), host=args.host, port=args.port)
            return 0
        if args.cmd == "insights":
            base = load_baseline(resolve_baseline_path(args.baseline_path))
            found = generate_insights(
                con, base, source=args.source, include_uncertain=args.all, scope=_scope(con, args)
            )
            if not found:
                print("Keine belegten Insights (zu wenige Daten oder keine eindeutigen Effekte).")
            for i in found:
                print(f"[{i.status}] {i.text}")
            print(f"Baseline {base.id} ({base.status}); regelbasiert, je Quelle getrennt.")
            return 0
        if args.cmd == "serve":
            con.close()
            import uvicorn

            from .web.app import create_app

            print("Datenbank geöffnet, baue die Oberfläche auf", flush=True)
            app = create_app(data, args.club_map, resolve_baseline_path(args.baseline_path))
            print(f"Oberfläche bereit, lausche auf {args.host}:{args.port}", flush=True)
            threading.Thread(target=app.state.warm_up, daemon=True, name="warm-up").start()
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            return 0
        if args.cmd == "arccos-check":
            return _arccos_check(data / "cache")
        if args.cmd == "reprocess":
            rep = reprocess_raw(con, API_CONNECTORS[args.source]())
            print(rep.as_text())
            _auto_dedup(con, args)
            return 0 if rep.status == "ok" else 2
        if args.cmd == "sync":
            connector = API_CONNECTORS[args.source]()
            if isinstance(connector, ArccosConnector):
                connector.cache_dir = data / "cache"
            if args.har is not None:
                if not isinstance(connector, ArccosConnector):
                    print("--har gilt nur für arccos", file=sys.stderr)
                    return 2
                connector.client = HarArccosClient(args.har)
            rep = sync_api(con, connector, raw_dir=data / "raw")
            print(rep.as_text())
            _auto_dedup(con, args)
            return 0 if rep.status == "ok" else 2
        if args.cmd == "import":
            code = 0
            for path in args.paths:
                for rep in sync_location(con, args.source, path, raw_dir=data / "raw"):
                    print(rep.as_text())
                    if rep.status in ("drift_detected", "error"):
                        code = 2
            _auto_dedup(con, args)
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
                min_speed_ratio=None if args.all_swings else DEFAULT_MIN_SPEED_RATIO,
            )
            print(
                f"{'Schläger':<15}{'Quelle':<14}{'Methode':<8}{'n':>4} {'Carry-Median':>13}"
                f" {'P10':>6} {'P90':>6} {'Bias':>7} {'Streu':>6}  Hinweis"
            )
            for s in rows:
                note = s.warning or ("gemischt" if s.mixed else "")
                print(
                    f"{s.club_type:<15}{s.source:<14}{s.method[:7]:<8}{s.n:>4} "
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
            min_speed_ratio=None if args.all_swings else DEFAULT_MIN_SPEED_RATIO,
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
