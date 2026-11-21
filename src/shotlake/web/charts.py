"""Serverseitige SVG-Diagramme (ohne JavaScript, Farben über CSS-Variablen)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape

from ..analytics.dispersion import ScatterGroup
from ..analytics.trend import TrendPoint

W, H = 640, 260
PAD_L, PAD_R, PAD_T, PAD_B = 44, 14, 14, 30


def _scale(lo: float, hi: float, a: float, b: float, v: float) -> float:
    return a if hi == lo else a + (v - lo) * (b - a) / (hi - lo)


@dataclass(frozen=True)
class Marker:
    """Notiz als senkrechte Markierung im Verlauf; Klick führt zum Vorher/Nachher-Vergleich."""

    when: date
    label: str
    kind: str  # Notiztyp (für die Farbe)
    href: str


@dataclass(frozen=True)
class PointInfo:
    """Tooltip-Text und Ziel eines Punkts im Verlauf (Klick öffnet die Runde)."""

    text: str
    href: str


def trend_svg(
    points: list[TrendPoint],
    window: int,
    markers: list[Marker] | None = None,
    info: dict[str, PointInfo] | None = None,
) -> str:
    """Differentials als Punkte (relevant gefüllt), gleitender Durchschnitt als Linie."""
    if not points:
        return ""
    t0 = min(p.start_utc for p in points).timestamp()
    t1 = max(p.start_utc for p in points).timestamp()
    values = [p.differential for p in points] + [
        p.moving_avg for p in points if p.moving_avg is not None
    ]
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.1, 1.0)
    lo, hi = lo - pad, hi + pad

    def x(p: TrendPoint) -> float:
        return _scale(t0, t1, PAD_L, W - PAD_R, p.start_utc.timestamp()) if t1 > t0 else W / 2

    def y(v: float) -> float:
        return _scale(lo, hi, H - PAD_B, PAD_T, v)

    parts = [
        f'<svg class="chart trend" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Score Differential mit gleitendem Durchschnitt über {window} Runden">'
    ]
    for i in range(5):  # Hilfslinien mit Beschriftung
        v = lo + (hi - lo) * i / 4
        parts.append(
            f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">'
            f"{v:.0f}</text>"
        )
    first, last = points[0].start_utc, points[-1].start_utc
    for label, anchor, px in ((first, "start", PAD_L), (last, "end", W - PAD_R)):
        parts.append(
            f'<text class="axis" x="{px}" y="{H - 8}" text-anchor="{anchor}">'
            f"{label:%d.%m.%Y}</text>"
        )
    for m in markers or []:
        stamp = datetime.combine(m.when, time.min).timestamp()
        if not t0 <= stamp <= t1 or t1 == t0:
            continue  # außerhalb des gezeigten Zeitraums
        mx = _scale(t0, t1, PAD_L, W - PAD_R, stamp)
        parts.append(
            f'<a href="{escape(m.href)}" class="marker {escape(m.kind)}">'
            f"<title>{escape(f'{m.when:%d.%m.%Y}: {m.label}')}</title>"
            f'<line x1="{mx:.1f}" x2="{mx:.1f}" y1="{PAD_T}" y2="{H - PAD_B}"/>'
            f'<text x="{mx:.1f}" y="{PAD_T + 2}" text-anchor="middle">&#9662;</text></a>'
        )
    avg = [(x(p), y(p.moving_avg)) for p in points if p.moving_avg is not None]
    if len(avg) >= 2:
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in avg)
        parts.append(f'<polyline class="avg" points="{path}"/>')
    for p in points:
        cls = "dot rel" if p.relevant else "dot"
        detail = (info or {}).get(p.round_id)
        title = escape(detail.text if detail else f"{p.start_utc:%d.%m.%Y}: {p.differential:.1f}")
        cx, cy = f"{x(p):.1f}", f"{y(p.differential):.1f}"
        dot = f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="4"/>'
        hit = f'<circle class="pt-hit" cx="{cx}" cy="{cy}" r="10"><title>{title}</title></circle>'
        if detail:
            parts.append(f'<a class="pt-link" href="{escape(detail.href)}">{dot}{hit}</a>')
        else:
            parts.append(f'<g class="pt-wrap">{dot}{hit}</g>')
    parts.append("</svg>")
    return "".join(parts)


def bars_svg(
    items: list[tuple[str, float]],
    unit: str = "",
    intervals: dict[str, tuple[float, float, int]] | None = None,
) -> str:
    """Horizontale Balken um die Nulllinie (SG je Kategorie); die Nulllinie wandert an den Rand,
    wenn alle Werte dasselbe Vorzeichen haben, damit die Breite genutzt wird.

    `intervals`: je Beschriftung (untere Grenze, obere Grenze, n) des 95-%-Intervalls; es wird als
    Strich mit Endkappen über dem Balken gezeichnet, der Tooltip nennt Werte und n."""
    if not items:
        return ""
    intervals = intervals or {}
    row_h, label_w, width, room = 30, 130, 520, 78  # room: Platz für den Wertetext am Balkenende
    reach = [v for _, v in items] + [b for lo_, hi_, _ in intervals.values() for b in (lo_, hi_)]
    lo, hi = min(0.0, *reach), max(0.0, *reach)
    span = max(hi - lo, 0.01)
    left, right = label_w + (room if lo < 0 else 0), width - (room if hi > 0 else 0)
    scale = (right - left) / span
    zero = left - lo * scale
    height = row_h * len(items) + 8
    out = [
        f'<svg class="chart bars" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Strokes Gained je Kategorie">',
        f'<line class="grid" x1="{zero:.1f}" x2="{zero:.1f}" y1="0" y2="{height}"/>',
    ]
    for i, (label, v) in enumerate(items):
        yy = 4 + i * row_h
        w = abs(v) * scale
        x0 = zero if v >= 0 else zero - w
        cls = "pos" if v >= 0 else "neg"
        ci = intervals.get(label)
        end = zero + w if v >= 0 else zero - w  # Balkenende
        text_x = end + 6 if v >= 0 else end - 6
        whisker = tip = ""
        if ci is not None:
            c_lo, c_hi, n = ci
            xl, xh = zero + c_lo * scale, zero + c_hi * scale
            mid = yy + 13
            whisker = (
                f'<g class="ci-bar"><line x1="{xl:.1f}" x2="{xh:.1f}" y1="{mid}" y2="{mid}"/>'
                f'<line x1="{xl:.1f}" x2="{xl:.1f}" y1="{mid - 5}" y2="{mid + 5}"/>'
                f'<line x1="{xh:.1f}" x2="{xh:.1f}" y1="{mid - 5}" y2="{mid + 5}"/></g>'
            )
            tip = (
                f"{label}: {v:+.2f}{unit}; 95-%-Intervall {c_lo:+.2f} bis {c_hi:+.2f}{unit}; "
                f"n = {n} Runden"
            )
            text_x = max(end, xh) + 6 if v >= 0 else min(end, xl) - 6
        out.append(
            f'<g class="bar-row">{f"<title>{escape(tip)}</title>" if tip else ""}'
            f'<rect class="hit" x="0" y="{yy}" width="{width}" height="{row_h}"/>'
            f'<text class="axis" x="{label_w - 8}" y="{yy + 17}" text-anchor="end">'
            f"{escape(label)}</text>"
            f'<rect class="bar {cls}" x="{x0:.1f}" y="{yy + 4}" width="{w:.1f}" '
            f'height="18" rx="2"/>{whisker}'
            f'<text class="val" x="{text_x:.1f}" '
            f'y="{yy + 17}" text-anchor="{"start" if v >= 0 else "end"}">{v:+.2f}{unit}</text></g>'
        )
    out.append("</svg>")
    return "".join(out)


@dataclass(frozen=True)
class BoxRow:
    """Eine Zeile des Boxplots (Werte in Metern)."""

    label: str
    n: int
    q1: float
    median: float
    q3: float
    whisker_lo: float
    whisker_hi: float
    outliers: tuple[float, ...]
    ok: bool = True  # genug Schläge für eine belastbare Darstellung
    median_ci: tuple[float, float] | None = None  # 95-%-Intervall des Medians


def box_row(
    label: str,
    values: Sequence[float],
    q1: float,
    median: float,
    q3: float,
    *,
    ok: bool = True,
    median_ci: tuple[float, float] | None = None,
) -> BoxRow:
    """Boxplot-Zeile nach Tukey: Antennen bis zum äußersten Wert innerhalb 1,5 x (Q3 - Q1)."""
    reach = 1.5 * (q3 - q1)
    inside = [v for v in values if q1 - reach <= v <= q3 + reach]
    low = min(inside) if inside else q1
    high = max(inside) if inside else q3
    outliers = tuple(v for v in values if v < q1 - reach or v > q3 + reach)
    return BoxRow(
        label, len(values), q1, median, q3, min(low, q1), max(high, q3), outliers, ok, median_ci
    )


def boxplot_svg(rows: list[BoxRow], factor: float = 1.0, unit: str = "m") -> str:
    """Boxplot der Schlagdistanz je Schläger (Reihenfolge wie im Bag).

    Kasten Q1 bis Q3, Strich = Median, Antennen bis zum äußersten Wert innerhalb 1,5 x Kasten,
    Punkte = Ausreißer. Der Median steht als Tooltip über der ganzen Zeile, nicht als Zahl.
    """
    if not rows:
        return ""
    row_h, label_w, width, right_pad = 30, 130, 680, 30
    top = max(max([r.whisker_hi, *r.outliers]) for r in rows) * factor
    step = 50 if top > 150 else 25
    hi = math.ceil(top / step) * step
    scale = (width - label_w - right_pad) / hi
    height = row_h * len(rows) + 30
    out = [
        f'<svg class="chart box" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Boxplot der Schlagdistanz je Schläger">'
    ]
    for t in range(0, int(hi) + 1, step):
        gx = label_w + t * scale
        out.append(
            f'<line class="grid" x1="{gx:.1f}" x2="{gx:.1f}" y1="0" y2="{height - 22}"/>'
            f'<text class="axis" x="{gx:.1f}" y="{height - 6}" text-anchor="middle">{t}</text>'
        )
    out.append(
        f'<text class="axis" x="{width - 2}" y="{height - 6}" text-anchor="end">{unit}</text>'
    )

    def x(v: float) -> float:
        return label_w + v * factor * scale

    for i, r in enumerate(rows):
        y = 4 + i * row_h
        mid = y + 13
        title = escape(
            f"{r.label}: Median {r.median * factor:.0f} {unit}"
            + (
                f" (95-%-Intervall {r.median_ci[0] * factor:.0f} bis {r.median_ci[1] * factor:.0f}"
                f" {unit})"
                if r.median_ci
                else ""
            )
            + "; "
            f"Kasten (Q1 bis Q3) {r.q1 * factor:.0f} bis {r.q3 * factor:.0f} {unit}; "
            f"Antennen {r.whisker_lo * factor:.0f} bis {r.whisker_hi * factor:.0f} {unit}; "
            f"Ausreißer {len(r.outliers)}; n = {r.n}"
        )
        parts = [
            f'<g class="{"" if r.ok else "thin"}"><title>{title}</title>',
            # unsichtbare Trefferfläche über die ganze Zeile: der Tooltip erscheint überall
            f'<rect class="hit" x="0" y="{y}" width="{width}" height="{row_h}"/>',
            f'<text class="axis" x="{label_w - 8}" y="{mid + 4}" text-anchor="end">'
            f"{escape(r.label)}</text>",
            f'<line class="whisker" x1="{x(r.whisker_lo):.1f}" x2="{x(r.q1):.1f}" '
            f'y1="{mid}" y2="{mid}"/>',
            f'<line class="whisker" x1="{x(r.q3):.1f}" x2="{x(r.whisker_hi):.1f}" '
            f'y1="{mid}" y2="{mid}"/>',
            f'<line class="whisker" x1="{x(r.whisker_lo):.1f}" x2="{x(r.whisker_lo):.1f}" '
            f'y1="{mid - 5}" y2="{mid + 5}"/>',
            f'<line class="whisker" x1="{x(r.whisker_hi):.1f}" x2="{x(r.whisker_hi):.1f}" '
            f'y1="{mid - 5}" y2="{mid + 5}"/>',
            f'<rect class="box-rect" x="{x(r.q1):.1f}" y="{mid - 8}" '
            f'width="{max(x(r.q3) - x(r.q1), 2):.1f}" height="16" rx="2"/>',
            f'<line class="med" x1="{x(r.median):.1f}" x2="{x(r.median):.1f}" '
            f'y1="{mid - 8}" y2="{mid + 8}"/>',
        ]
        parts += [
            f'<circle class="box-out" cx="{x(v):.1f}" cy="{mid}" r="2.6"/>' for v in r.outliers
        ]
        parts.append("</g>")
        out.append("".join(parts))
    out.append("</svg>")
    return "".join(out)


def ci_svg(effect: float, low: float, high: float, bound: float, good: bool | None = None) -> str:
    """Effekt mit 95-%-Intervall auf einer Achse um 0 (gemeinsame Skala `-bound` bis `bound`)."""
    w, h, pad = 340, 26, 8
    bound = max(bound, 1e-9)

    def x(v: float) -> float:
        return pad + (v + bound) / (2 * bound) * (w - 2 * pad)

    lo, hi = sorted((low, high))
    cls = "pos" if (effect >= 0 if good is None else good) else "neg"
    return (
        f'<svg class="chart ci" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Effekt {effect:+.1f}, 95-%-Intervall {lo:.1f} bis {hi:.1f}">'
        f'<line class="grid" x1="{pad}" x2="{w - pad}" y1="13" y2="13"/>'
        f'<line class="zero" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="3" y2="23"/>'
        f'<line class="whisker {cls}" x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="13" y2="13"/>'
        f'<circle class="est {cls}" cx="{x(effect):.1f}" cy="13" r="4.5"/></svg>'
    )


def _nice_step(span: float, max_ticks: int = 4) -> float:
    """Schrittweite für Achsenmarken: kleinster 'runder' Wert mit höchstens `max_ticks` Marken."""
    for step in (1, 2, 5, 10, 20, 25, 50, 100):
        if span / step <= max_ticks:
            return float(step)
    return 100.0


def scatter_svg(group: ScatterGroup, factor: float = 1.0, unit: str = "m") -> str:
    """Streuung: links/rechts waagerecht, Länge (Carry) senkrecht, gleicher Maßstab, 95-%-Ellipse.

    Beide Achsen tragen Zahlen und Einheit; jeder Punkt hat einen Tooltip mit Länge und seitlicher
    Abweichung (mit Richtung), die Ellipse einen mit Mittelpunkt und Halbachsen.
    """
    pts = [(x * factor, y * factor) for x, y in group.points]
    ell = [(x * factor, y * factor) for x, y in group.ellipse.outline()] if group.ellipse else []
    every = pts + ell
    half = max(max(abs(x) for x, _ in every), 5.0 * factor) * 1.1
    y_lo, y_hi = min(y for _, y in every), max(y for _, y in every)
    y_pad = max((y_hi - y_lo) * 0.08, 2.0 * factor)
    y_lo, y_hi = y_lo - y_pad, y_hi + y_pad
    w, h, pl, pr, pt, pb = 420, 372, 52, 16, 26, 56
    scale = min((w - pl - pr) / (2 * half), (h - pt - pb) / (y_hi - y_lo))
    cx = pl + (w - pl - pr) / 2

    def sx(v: float) -> float:
        return cx + v * scale

    def sy(v: float) -> float:
        return h - pb - (v - y_lo) * scale

    def side_text(value: float) -> str:
        return f"{abs(value):.1f} {unit} {'rechts' if value >= 0 else 'links'}"

    out = [
        f'<svg class="chart scatter" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Streuung {escape(group.club_type)} ({escape(group.source)}, '
        f'{escape(group.method)}), {len(pts)} Schläge">',
        f'<text class="axis" x="2" y="12" text-anchor="start">Länge ({unit})</text>',
    ]
    y_step = 10 if (y_hi - y_lo) > 40 * factor else 5
    v = math.ceil(y_lo / (y_step * factor)) * y_step * factor
    while v <= y_hi:  # Längenachse
        out.append(
            f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
            f'<text class="axis" x="{pl - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.0f}</text>'
        )
        v += y_step * factor
    x_step = _nice_step(
        half
    )  # Seitenachse: Marken beidseits der Mitte, Beschriftung ohne Vorzeichen
    k = 1
    out.append(f'<line class="grid mid" x1="{cx:.1f}" x2="{cx:.1f}" y1="{pt}" y2="{h - pb}"/>')
    while k * x_step <= half:
        for sign in (-1, 1):
            gx = sx(sign * k * x_step)
            out.append(
                f'<line class="grid" x1="{gx:.1f}" x2="{gx:.1f}" y1="{pt}" y2="{h - pb}"/>'
                f'<text class="axis" x="{gx:.1f}" y="{h - pb + 16}" text-anchor="middle">'
                f"{k * x_step:g}</text>"
            )
        k += 1
    out.append(
        f'<text class="axis" x="{cx:.1f}" y="{h - pb + 16}" text-anchor="middle">0</text>'
        f'<text class="axis" x="{pl}" y="{h - 8}" text-anchor="start">&#8592; links</text>'
        f'<text class="axis" x="{cx:.1f}" y="{h - 8}" text-anchor="middle">seitlich ({unit})</text>'
        f'<text class="axis" x="{w - pr}" y="{h - 8}" text-anchor="end">rechts &#8594;</text>'
    )
    if group.ellipse and ell:
        e = group.ellipse
        title = escape(
            f"95-%-Ellipse: Mitte Länge {e.cy * factor:.0f} {unit}, seitlich "
            f"{side_text(e.cx * factor)}; Halbachsen {e.semi_major * factor:.0f} und "
            f"{e.semi_minor * factor:.0f} {unit}"
        )
        path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in ell)
        out.append(f'<polygon class="ell" points="{path}"><title>{title}</title></polygon>')
    for x, y in pts:
        title = escape(f"Länge {y:.0f} {unit}, seitlich {side_text(x)}")
        out.append(
            f'<g class="pt-wrap"><circle class="pt" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3"/>'
            f'<circle class="pt-hit" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="9">'
            f"<title>{title}</title></circle></g>"
        )
    out.append("</svg>")
    return "".join(out)


def fmt_date(d: datetime) -> str:
    return f"{d:%d.%m.%Y}"
