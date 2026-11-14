"""Serverseitige SVG-Diagramme (ohne JavaScript, Farben über CSS-Variablen)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape

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
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
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
    """Horizontale Balken um die Nulllinie (SG je Kategorie); negativ links."""
    if not items:
        return ""
    row_h, label_w, width = 30, 130, 520
    span = max(max(abs(v) for _, v in items), 0.01)
    zero = label_w + (width - label_w) / 2
    half = (width - label_w) / 2 - 40
    height = row_h * len(items) + 8
    out = [
        f'<svg class="chart bars" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Strokes Gained je Kategorie">',
        f'<line class="grid" x1="{zero}" x2="{zero}" y1="0" y2="{height}"/>',
    ]
    for i, (label, v) in enumerate(items):
        yy = 4 + i * row_h
        w = abs(v) / span * half
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


def fmt_date(d: datetime) -> str:
    return f"{d:%d.%m.%Y}"
