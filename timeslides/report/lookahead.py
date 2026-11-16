"""The Look-ahead: each track projected past the end of the window.

The waterfall used to stop at "now", and its one forward-looking number, the
time for two objects to align, came from a single straight-line fit across the
whole window. A manoeuvre was therefore averaged into a rate the object never
flew: in the proximity-operations scenario in tests/test_lookahead.py, a chaser
that raised its closing rate from about 18 to 87 s/day showed +45, and the time
to align came out at roughly twice the truth.

This module does three things, all server-side so they are testable and work
in a downloaded copy of the report:

  * Finds a change of drift rate in each track: one straight line against two
    lines joined at a single break, compared by BIC. Flagged only when the
    evidence is strong AND the rate moves by at least MIN_RATE_CHANGE, because
    on clean data the statistics alone call tiny curvature significant (the
    prototype flagged a 0.1 s/day "change" before the second condition).
  * Forecasts each track as a straight line through the data since its last
    change, with a band combining the fit's own scatter and a stated model
    allowance, RATE_ALLOWANCE, that widens with time.
  * Solves where every pair of forecast lines meets, with the time's own band.

The output is a set of ready-made views, one per horizon, holding Plotly
traces, shapes, annotations and the rail's event list, so the browser only
chooses one and draws it.

Wording follows a deliberate rule. The plot says "rate change", which is what
the data shows. "Probable manoeuvre" appears only in the rail's explanatory
line, so a screenshot dropped into a briefing does not assert intent.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from .builder import esc

HORIZONS = (3, 7, 14)
DEFAULT_HORIZON = 7
MAX_PASSES = 8               # passes listed per view, nearest first
FAR_LIMIT_DAYS = 30.0        # how far beyond the horizon the next pass is named

MIN_RATE_CHANGE = 2.0        # s/day: smaller changes are not reported
MIN_BIC_GAIN = 10.0          # "very strong" evidence on the usual BIC scale
MIN_SIDE = 8                 # points either side of a break, at least
MAX_BREAK_CANDIDATES = 60    # break times tried per track; keeps a long window cheap

# Model allowance on every forecast drift, in s/day, applied in quadrature with
# the fit's scatter. The scatter alone gives pass times to the half hour on
# clean data, which would be read as a promise; this stands in for drag and
# model error until an analyst has set it against real tenant data.
RATE_ALLOWANCE = 1.0

MIN_CLOSING = 0.05           # s/day: the client's own STEADY threshold
ALIGNED_S = 1.0              # s: the client's own ALIGNED threshold
SAMPLES_PER_DAY = 2

CHANGE_COLOUR = "#E0952A"
PASS_COLOUR = "#E6ECF5"
NOW_COLOUR = "#C67C00"
FONT = "ui-monospace,Menlo,monospace"


@dataclass
class Change:
    t: float          # days relative to now (negative: in the window)
    x: float          # offset at the break, s
    before: float     # s/day
    after: float      # s/day


@dataclass
class Track:
    norad: int
    name: str
    colour: str
    is_ref: bool
    a: float = 0.0     # offset at now, s
    b: float = 0.0     # drift, s/day
    s: float = 0.0     # residual scatter, s
    tbar: float = 0.0
    sxx: float = 1.0
    m: int = 0
    change: Change | None = None


@dataclass
class Pass:
    t: float           # days from now
    x: float           # offset where the lines meet, s
    mover: str
    other: str
    half: float        # half-width of the time band, days


def esc_plot(value) -> str:
    """For Plotly text. Plotly decodes &amp; &lt; &gt; and &quot; only, so the
    apostrophe esc() turns into &#x27; would be shown as those six characters.
    An escaper belongs to a sink, not to a project."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------- #
#  Fitting
# --------------------------------------------------------------------------- #
def fit_line(t: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float, float]:
    """Least-squares y = a + b t -> (a, b, scatter, mean t, Sxx)."""
    tbar = float(t.mean())
    sxx = float(((t - tbar) ** 2).sum())
    b = float(((t - tbar) * (y - y.mean())).sum() / sxx)
    a = float(y.mean() - b * tbar)
    resid = y - (a + b * t)
    s = math.sqrt(float(resid @ resid) / max(1, len(t) - 2))
    return a, b, s, tbar, sxx


def _rss(design: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    return float(resid @ resid), coef


def find_rate_change(t: np.ndarray, y: np.ndarray) -> Change | None:
    """The single best change of drift rate, or None.

    Two lines joined at the break, so position is continuous and only the rate
    changes, which is what a burn does to along-track timing. Candidate breaks
    are spread through the interior, leaving MIN_SIDE points either side.
    """
    n = len(t)
    if n < 2 * MIN_SIDE + 1:
        return None
    ones = np.ones_like(t)
    rss1, _ = _rss(np.vstack([ones, t]).T, y)
    ordered = np.sort(t)[MIN_SIDE:-MIN_SIDE]
    step = max(1, len(ordered) // MAX_BREAK_CANDIDATES)
    best = None
    for tb in ordered[::step]:
        rss2, coef = _rss(np.vstack([ones, t, np.maximum(0.0, t - tb)]).T, y)
        if best is None or rss2 < best[0]:
            best = (rss2, float(tb), coef)
    rss2, tb, coef = best
    floor = 1e-12                       # a perfect fit has no log
    gain = (n * math.log(max(rss1, floor) / n) + 2 * math.log(n)) - \
           (n * math.log(max(rss2, floor) / n) + 4 * math.log(n))
    if gain <= MIN_BIC_GAIN or abs(float(coef[2])) < MIN_RATE_CHANGE:
        return None
    return Change(t=tb, x=float(coef[0] + coef[1] * tb),
                  before=float(coef[1]), after=float(coef[1] + coef[2]))


def track_for(norad, name, colour, is_ref, points, now: dt.datetime) -> Track | None:
    """A forecastable track from [(epoch, offset)], or None if it is not one."""
    if is_ref:
        return Track(norad, name, colour, True, m=len(points))
    if len(points) < 3:
        return None
    t = np.array([(e - now).total_seconds() / 86400.0 for e, _ in points])
    y = np.array([float(v) for _, v in points])
    if float(t.max() - t.min()) <= 0.0:
        return None
    change = find_rate_change(t, y)
    use = t >= change.t if change else np.ones_like(t, dtype=bool)
    a, b, s, tbar, sxx = fit_line(t[use], y[use])
    return Track(norad, name, colour, False, a, b, s, tbar, sxx, int(use.sum()), change)


def half_width(track: Track, t: float) -> float:
    """95% half-width in seconds at t days from now: the fit's prediction
    interval and the model allowance, in quadrature."""
    if track.is_ref:
        return 0.0
    stat = 2.0 * track.s * math.sqrt(1 + 1 / track.m + (t - track.tbar) ** 2 / track.sxx)
    return math.hypot(stat, RATE_ALLOWANCE * t)


def passes(tracks: list[Track], limit_days: float) -> list[Pass]:
    """Where each pair's forecast lines meet, after now and within the limit."""
    out = []
    for i, p in enumerate(tracks):
        for q in tracks[i + 1:]:
            gap, closing = p.a - q.a, p.b - q.b
            if abs(closing) < MIN_CLOSING or abs(gap) < ALIGNED_S:
                continue
            t = -gap / closing
            if not 0.0 < t <= limit_days:
                continue
            mover, other = (p, q) if abs(p.b) >= abs(q.b) else (q, p)
            half = math.hypot(half_width(p, t), half_width(q, t)) / abs(closing)
            out.append(Pass(t=t, x=p.a + p.b * t, mover=mover.name, other=other.name,
                            half=half))
    return sorted(out, key=lambda e: e.t)


# --------------------------------------------------------------------------- #
#  Views
# --------------------------------------------------------------------------- #
def _when(now: dt.datetime, days: float) -> str:
    return (now + dt.timedelta(days=days)).strftime("%d %b %H:%MZ")


def _iso(now: dt.datetime, days: float) -> str:
    return (now + dt.timedelta(days=days)).isoformat()


def _band_text(half_days: float) -> str:
    minutes = half_days * 1440.0
    if minutes < 90:
        return f"± {minutes:.0f} min"
    if minutes < 2880:
        return f"± {minutes / 60:.1f} h"
    return f"± {half_days:.1f} d"


def _projection(track: Track, now: dt.datetime, horizon: int) -> list[dict]:
    steps = np.linspace(0.0, horizon, horizon * SAMPLES_PER_DAY + 1)
    ys = [_iso(now, float(k)) for k in steps]
    mid = [round(track.a + track.b * float(k), 2) for k in steps]
    half = [round(half_width(track, float(k)), 2) for k in steps]
    name = esc_plot(track.name)
    return [
        {"type": "scatter", "mode": "lines", "uid": f"ahead-band-{track.norad}",
         "x": [m + h for m, h in zip(mid, half, strict=True)]
              + [m - h for m, h in zip(mid[::-1], half[::-1], strict=True)],
         "y": ys + ys[::-1], "fill": "toself", "fillcolor": track.colour + "22",
         "line": {"width": 0}, "hoverinfo": "skip", "showlegend": False},
        {"type": "scatter", "mode": "lines", "uid": f"ahead-line-{track.norad}",
         "x": mid, "y": ys, "customdata": half, "showlegend": False,
         "line": {"color": track.colour, "width": 2, "dash": "dot"},
         "hovertemplate": f"<b>{name}</b> · forecast<br>%{{y|%d %b %H:%M}}Z<br>"
                          "offset %{x:.0f} s ± %{customdata:.0f} s<extra></extra>"},
    ]


def _markers(uid: str, xs, ys, symbol: str, colour: str, size: int) -> dict:
    return {"type": "scatter", "mode": "markers", "uid": uid, "x": xs, "y": ys,
            "showlegend": False, "hoverinfo": "skip",
            "marker": {"symbol": symbol, "size": size, "color": colour,
                       "line": {"width": 2, "color": colour if symbol.endswith("open")
                                else "#0B1020"}}}


def _annotation(x, y, text: str, ax: int, colour: str, filled: bool) -> dict:
    return {"x": x, "y": y, "text": text, "showarrow": True, "arrowcolor": colour,
            "ax": ax, "ay": 0, "align": "left", "borderpad": 5,
            "font": {"color": "#0B1020" if filled else colour, "size": 10.5, "family": FONT},
            "bgcolor": colour if filled else "rgba(13,20,40,0.92)",
            "bordercolor": colour}


def _change_item(track: Track, now: dt.datetime) -> str:
    c = track.change
    return (f'<li class="ev ev-change"><span class="evtag">RATE CHANGE</span>'
            f"<b>{esc(track.name)}</b> drift {c.before:+.0f} → {c.after:+.0f} s/day "
            f"around {_when(now, c.t)}"
            '<span class="evnote">probable manoeuvre · forecast uses only the data after it</span></li>')


def _pass_item(e: Pass, now: dt.datetime, far: bool) -> str:
    cls = "ev ev-far" if far else "ev"
    return (f'<li class="{cls}"><span class="evwhen">in {e.t:.1f} d</span>'
            f"<b>{esc(e.mover)}</b> passes <b>{esc(e.other)}</b>"
            f'<span class="evnote">{_when(now, e.t)} {_band_text(e.half)}</span></li>')


def view(tracks: list[Track], now: dt.datetime, horizon: int) -> dict:
    """Everything one horizon draws: traces, shapes, annotations, the list."""
    moving = [t for t in tracks if not t.is_ref]
    changed = [t for t in moving if t.change]
    every = passes(tracks, FAR_LIMIT_DAYS)
    inside = [e for e in every if e.t <= horizon][:MAX_PASSES]
    beyond = [e for e in every if e.t > horizon][:1]

    traces = [tr for t in moving for tr in _projection(t, now, horizon)]
    traces.append(_markers("ahead-passes", [round(e.x, 2) for e in inside],
                           [_iso(now, e.t) for e in inside], "circle-open", PASS_COLOUR, 22))
    traces.append(_markers("ahead-changes", [round(t.change.x, 2) for t in changed],
                           [_iso(now, t.change.t) for t in changed], "diamond", CHANGE_COLOUR, 15))
    annotations = [
        {"xref": "paper", "x": 0.995, "xanchor": "right", "y": now.isoformat(),
         "yanchor": "bottom", "showarrow": False, "text": f"NOW · {_when(now, 0)}",
         "font": {"color": NOW_COLOUR, "size": 10.5, "family": FONT}},
        {"xref": "paper", "x": 0.005, "xanchor": "left", "y": now.isoformat(),
         "yanchor": "top", "yshift": -4, "showarrow": False,
         "text": f"LOOK-AHEAD · {horizon} DAYS · straight-line forecast, "
                 "assumes no further manoeuvre",
         # backed, because a forecast line can run straight through it
         "bgcolor": "rgba(13,20,40,0.92)", "borderpad": 3,
         "font": {"color": "#739BCF", "size": 10.5, "family": FONT}},
    ]
    annotations += [
        _annotation(round(e.x, 2), _iso(now, e.t),
                    f"{esc_plot(e.mover)} passes {esc_plot(e.other)}<br>"
                    f"in {e.t:.1f} d  {_band_text(e.half)}",
                    110 if e.x <= 0 else -110, PASS_COLOUR, filled=False)
        for e in inside]
    annotations += [
        _annotation(round(t.change.x, 2), _iso(now, t.change.t),
                    f"{esc_plot(t.name)} · rate change<br>"
                    f"{t.change.before:+.0f} → {t.change.after:+.0f} s/day",
                    -140 if t.change.x <= 0 else 140, CHANGE_COLOUR, filled=True)
        for t in changed]
    shapes = [
        {"type": "rect", "xref": "paper", "x0": 0, "x1": 1, "y0": now.isoformat(),
         "y1": _iso(now, horizon), "fillcolor": "rgba(56,95,175,0.10)",
         "line": {"width": 0}, "layer": "below"},
        {"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": now.isoformat(),
         "y1": now.isoformat(), "line": {"color": NOW_COLOUR, "width": 1.5, "dash": "dash"}},
    ]
    items = [_change_item(t, now) for t in changed]
    items += [_pass_item(e, now, far=False) for e in inside]
    items += [_pass_item(e, now, far=True) for e in beyond]
    if not changed and not inside:
        items.insert(0, '<li class="ev ev-quiet"><b>Nothing passes and no rate changes</b> '
                        f"in the next {horizon} days."
                        '<span class="evnote">every track holds its current drift</span></li>')
    return {"traces": traces, "shapes": shapes, "annotations": annotations,
            "items": "".join(items)}


OFF_VIEW = {"traces": [], "shapes": [], "annotations": [],
            "items": '<li class="ev ev-quiet">Off. Choose a horizon to see where each '
                     "track is heading.</li>"}


def build(objects: list[dict], now: dt.datetime) -> tuple[dict, dict]:
    """objects: [{norad, name, colour, is_ref, points: [(epoch, offset)]}].

    -> ({"views": {"0": .., "3": .., "7": .., "14": ..}, "default": "7"},
        {norad: {"rate": s/day now, "was": s/day before}}).
    The second is for the cards, so a track that changed rate shows the rate
    it has flown since, the one its forecast uses, not an average across both.
    """
    tracks = [t for t in (track_for(o["norad"], o["name"], o["colour"], o["is_ref"],
                                    o["points"], now) for o in objects) if t]
    views = {"0": OFF_VIEW, **{str(h): view(tracks, now, h) for h in HORIZONS}}
    changes = {t.norad: {"rate": t.b, "was": t.change.before} for t in tracks if t.change}
    return {"views": views, "default": str(DEFAULT_HORIZON)}, changes
