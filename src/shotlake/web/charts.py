"""Serverseitige SVG-Diagramme (ohne JavaScript, Farben über CSS-Variablen)."""

from __future__ import annotations

import math
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


def trend_svg(points: list[TrendPoint], window: int, markers: list[Marker] | None = None) -> str:
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
        title = escape(f"{p.start_utc:%d.%m.%Y}: {p.differential:.1f}")
        parts.append(f'<circle class="{cls}" cx="{x(p):.1f}" cy="{y(p.differential):.1f}" r="4">'
                     f"<title>{title}</title></circle>")  # fmt: skip
    parts.append("</svg>")
    return "".join(parts)


def bars_svg(items: list[tuple[str, float]], unit: str = "") -> str:
    """Horizontale Balken um die Nulllinie (SG je Kategorie); die Nulllinie wandert an den Rand,
    wenn alle Werte dasselbe Vorzeichen haben, damit die Breite genutzt wird."""
    if not items:
        return ""
    row_h, label_w, width, room = 30, 130, 520, 78  # room: Platz für den Wertetext am Balkenende
    lo, hi = min(0.0, *(v for _, v in items)), max(0.0, *(v for _, v in items))
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
        out.append(
            f'<text class="axis" x="{label_w - 8}" y="{yy + 17}" text-anchor="end">'
            f"{escape(label)}</text>"
            f'<rect class="bar {cls}" x="{x0:.1f}" y="{yy + 4}" width="{w:.1f}" '
            f'height="18" rx="2"/>'
            f'<text class="val" x="{(zero + w + 6) if v >= 0 else (zero - w - 6):.1f}" '
            f'y="{yy + 17}" text-anchor="{"start" if v >= 0 else "end"}">{v:+.2f}{unit}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def reach_svg(
    items: list[tuple[str, float, float, float, int, bool]], factor: float = 1.0, unit: str = "m"
) -> str:
    """Distanzbereich je Schläger: Balken P10 bis P90, Strich = Median (Reihenfolge wie im Bag).

    Eintrag: (Schläger, P10, Median, P90, n, belastbar); Werte in Metern, `factor` rechnet um.
    """
    if not items:
        return ""
    row_h, label_w, width, right_pad = 30, 150, 680, 46
    top = max(p90 for _, _, _, p90, _, _ in items) * factor
    step = 50 if top > 150 else 25
    hi = math.ceil(top / step) * step
    scale = (width - label_w - right_pad) / hi
    height = row_h * len(items) + 30
    out = [
        f'<svg class="chart reach" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Distanzbereich je Schläger (P10 bis P90, Median als Strich)">'
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
    for i, (label, p10, med, p90, n, ok) in enumerate(items):
        yy = 4 + i * row_h
        a, m, b = (label_w + v * factor * scale for v in (p10, med, p90))
        title = escape(f"{label}: Median {med * factor:.0f} {unit}, P10 {p10 * factor:.0f}, "
                       f"P90 {p90 * factor:.0f}, n = {n}")  # fmt: skip
        out.append(
            f'<g class="{"" if ok else "thin"}"><title>{title}</title>'
            f'<text class="axis" x="{label_w - 8}" y="{yy + 17}" text-anchor="end">'
            f"{escape(label)}</text>"
            f'<rect class="reach-bar" x="{a:.1f}" y="{yy + 5}" width="{max(b - a, 2):.1f}" '
            f'height="16" rx="3"/>'
            f'<line class="med" x1="{m:.1f}" x2="{m:.1f}" y1="{yy + 2}" y2="{yy + 24}"/>'
            f'<text class="val" x="{b + 6:.1f}" y="{yy + 17}">{med * factor:.0f}</text></g>'
        )
    out.append("</svg>")
    return "".join(out)


def ci_svg(effect: float, low: float, high: float, bound: float) -> str:
    """Effekt mit 95-%-Intervall auf einer Achse um 0 (gemeinsame Skala `-bound` bis `bound`)."""
    w, h, pad = 340, 26, 8
    bound = max(bound, 1e-9)

    def x(v: float) -> float:
        return pad + (v + bound) / (2 * bound) * (w - 2 * pad)

    lo, hi = sorted((low, high))
    cls = "pos" if effect >= 0 else "neg"
    return (
        f'<svg class="chart ci" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Effekt {effect:+.1f}, 95-%-Intervall {lo:.1f} bis {hi:.1f}">'
        f'<line class="grid" x1="{pad}" x2="{w - pad}" y1="13" y2="13"/>'
        f'<line class="zero" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="3" y2="23"/>'
        f'<line class="whisker {cls}" x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="13" y2="13"/>'
        f'<circle class="est {cls}" cx="{x(effect):.1f}" cy="13" r="4.5"/></svg>'
    )


def scatter_svg(group: ScatterGroup, factor: float = 1.0, unit: str = "m") -> str:
    """Streuung: links/rechts waagerecht, Carry senkrecht, gleicher Maßstab, 95-%-Ellipse."""
    pts = [(x * factor, y * factor) for x, y in group.points]
    ell = [(x * factor, y * factor) for x, y in group.ellipse.outline()] if group.ellipse else []
    every = pts + ell
    half = max(max(abs(x) for x, _ in every), 5.0 * factor) * 1.1
    y_lo, y_hi = min(y for _, y in every), max(y for _, y in every)
    y_pad = max((y_hi - y_lo) * 0.08, 2.0 * factor)
    y_lo, y_hi = y_lo - y_pad, y_hi + y_pad
    w, h, pl, pr, pt, pb = 420, 340, 44, 14, 12, 28
    scale = min((w - pl - pr) / (2 * half), (h - pt - pb) / (y_hi - y_lo))
    cx = pl + (w - pl - pr) / 2

    def sx(v: float) -> float:
        return cx + v * scale

    def sy(v: float) -> float:
        return h - pb - (v - y_lo) * scale

    out = [
        f'<svg class="chart scatter" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Streuung {escape(group.club_type)} ({escape(group.source)}, '
        f'{escape(group.method)}), {len(pts)} Schläge">',
        f'<line class="grid" x1="{cx:.1f}" x2="{cx:.1f}" y1="{pt}" y2="{h - pb}"/>',
    ]
    step = 10 if (y_hi - y_lo) > 40 * factor else 5
    v = math.ceil(y_lo / (step * factor)) * step * factor
    while v <= y_hi:
        out.append(
            f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
            f'<text class="axis" x="{pl - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.0f}</text>'
        )
        v += step * factor
    out.append(
        f'<text class="axis" x="{pl}" y="{h - 8}" text-anchor="start">links</text>'
        f'<text class="axis" x="{w - pr}" y="{h - 8}" text-anchor="end">rechts</text>'
        f'<text class="axis" x="{cx:.1f}" y="{h - 8}" text-anchor="middle">{unit}</text>'
    )
    if ell:
        path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in ell)
        out.append(f'<polygon class="ell" points="{path}"/>')
    for x, y in pts:
        title = escape(f"Carry {y:.0f} {unit}, Seite {x:+.1f} {unit}")
        out.append(
            f'<circle class="pt" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3">'
            f"<title>{title}</title></circle>"
        )
    out.append("</svg>")
    return "".join(out)


def fmt_date(d: datetime) -> str:
    return f"{d:%d.%m.%Y}"
