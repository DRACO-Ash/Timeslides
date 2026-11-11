"""A hand-entered state vector, placed on a report's waterfall for comparison.

An operator sometimes holds a state that did not come from the UDL: from
another system, a manoeuvre plan, a colleague's solution. The question is
where it lands against what the UDL says. This module answers that one
question and nothing else.

What makes it safe to offer is what it deliberately does not do:

  * It is never mixed into a report's data. The offset is computed against the
    reference element set the report itself used, which the report carries for
    each data mode and reference, and returned as a separate trace. The report
    file on the volume is not touched.
  * It is not stored. The server holds nothing between requests, the page holds
    it in memory only, and a reload or a new render drops it.
  * It cannot be mistaken for UDL data. The trace is a shape and colour no
    provider uses, it is labelled on the chart itself so a PNG export carries
    the label, and its tooltip says so.

The offset is computed by exactly the path the report uses for a UDL state
vector: the reference element set propagated to the state's epoch, the state
converted to TEME if it is J2000, and the same along-track projection. A test
holds the two paths to agree, so a manual state equal to a plotted UDL state
lands on that point.

Inputs arrive as strings from a form and are parsed here, so every refusal is a
sentence that says what to fix rather than a schema error.
"""

from __future__ import annotations

import datetime as dt
import math
import string
import uuid

import numpy as np

from .errors import ValidationError
from .models import Elset, StateVector
from .physics import along_track_offset, propagate, to_teme
from .pipeline import MAX_WINDOW_DAYS
from .report.builder import esc

FRAMES = ("J2000", "TEME")

# Not a provider colour, not the reference copper, and not in the object
# palette, so the trace cannot be read as any object's data.
MANUAL_COLOUR = "#FFFFFF"
MANUAL_RING = "#C67C00"

# Bounds that catch the commonest entry mistake, the wrong unit, with a message
# that names it. A position in metres is thousands of times too large and a
# velocity in metres per second likewise, so both fall outside these.
MIN_RADIUS_KM = 6378.137          # the Earth's equatorial radius
MAX_RADIUS_KM = 100_000.0
MAX_SPEED_KMS = 20.0

LABEL_MAX = 40
# A short note, so a deliberately plain alphabet. Nothing in it needs escaping
# in either the rail or the chart's tooltip, which removes the question of
# which escaper the charting sink wants rather than answering it twice.
LABEL_CHARS = frozenset(string.ascii_letters + string.digits + " .,:;/()#_-")

TLE_LINE_LEN = 69

_J2000_JD = 2451545.0
_J2000_UTC = dt.datetime(2000, 1, 1, 12)


def _number(name: str, raw, unit: str) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValidationError(f"{name} must be a number in {unit}") from None
    if not math.isfinite(value):
        raise ValidationError(f"{name} must be a finite number in {unit}")
    return value


def _vector(raw: dict, names: tuple, unit: str) -> np.ndarray:
    return np.array([_number(n, raw.get(n, ""), unit) for n in names])


def parse_epoch(raw) -> dt.datetime:
    """An ISO 8601 UTC epoch -> naive UTC, keeping fractions of a second.

    Sub-second precision is not decoration here. At LEO speed a second of
    epoch error is about 7.6 km along-track, which is a full second of offset,
    so the fraction the operator typed is kept rather than truncated.
    """
    text = str(raw).strip()
    if not text:
        raise ValidationError("epoch is required, for example 2026-06-25T12:00:00Z")
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValidationError(
            f"epoch {text[:40]!r} is not an ISO 8601 time, for example "
            "2026-06-25T12:00:00Z") from None
    if when.tzinfo is not None:
        when = when.astimezone(dt.UTC).replace(tzinfo=None)
    return when


def parse_frame(raw) -> str:
    frame = str(raw or "J2000").strip().upper()
    if frame not in FRAMES:
        raise ValidationError(f"frame must be one of {', '.join(FRAMES)}")
    return frame


def parse_label(raw) -> str:
    label = str(raw or "").strip()
    if len(label) > LABEL_MAX:
        raise ValidationError(f"label is limited to {LABEL_MAX} characters")
    bad = sorted({c for c in label if c not in LABEL_CHARS})
    if bad:
        raise ValidationError(
            "label may use letters, digits, spaces and . , : ; / ( ) # _ - only; "
            f"remove {''.join(bad)[:10]!r}")
    return label


def check_state(r: np.ndarray, v: np.ndarray) -> None:
    """Refuse a state that cannot be an Earth orbit, naming the likely cause."""
    radius = float(np.linalg.norm(r))
    speed = float(np.linalg.norm(v))
    if radius > MAX_RADIUS_KM:
        raise ValidationError(
            f"position magnitude is {radius:,.0f} km, beyond {MAX_RADIUS_KM:,.0f} km. "
            "Positions are in kilometres; this looks like metres.")
    if radius <= MIN_RADIUS_KM:
        raise ValidationError(
            f"position magnitude is {radius:,.1f} km, inside the Earth. "
            "Positions are in kilometres from the Earth's centre.")
    if speed > MAX_SPEED_KMS:
        raise ValidationError(
            f"velocity magnitude is {speed:,.1f} km/s, beyond {MAX_SPEED_KMS:.0f} km/s. "
            "Velocities are in km/s; this looks like m/s.")
    if speed == 0.0:
        raise ValidationError("velocity is zero; a state needs a velocity in km/s")


def parse_reference(raw) -> Elset:
    """The report's own reference element set, as the page sent it back.

    It comes from the report payload, but it crosses the HTTP boundary like
    anything else, so its shape is checked before the propagator sees it.
    """
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        raise ValidationError("the reference element set must be two TLE lines")
    line1, line2 = (str(x) for x in raw)
    if (len(line1) != TLE_LINE_LEN or len(line2) != TLE_LINE_LEN
            or not line1.startswith("1 ") or not line2.startswith("2 ")):
        raise ValidationError("the reference element set is not a two-line element set")
    # The parser does not raise on a malformed set: it returns a satellite with
    # a non-zero error and a meaningless epoch, so the error is what is checked.
    sat = Elset(epoch=_J2000_UTC, line1=line1, line2=line2).satrec()
    if sat.error:
        raise ValidationError("the reference element set could not be parsed")
    epoch = _J2000_UTC + dt.timedelta(days=sat.jdsatepoch + sat.jdsatepochF - _J2000_JD)
    return Elset(epoch=epoch, line1=line1, line2=line2)


def manual_offset(sv: StateVector, ref: Elset, invert: bool) -> tuple[float, float]:
    """(offset in seconds, reference speed in km/s) for one state.

    The same three steps as physics._state_offsets, for one vector rather than
    a series: propagate the reference to the state's epoch, take the state to
    TEME, project onto the reference's direction of travel.
    """
    gap = abs((sv.epoch - ref.epoch).total_seconds()) / 86400.0
    if gap > MAX_WINDOW_DAYS:
        raise ValidationError(
            f"epoch is {gap:,.0f} days from the reference element set "
            f"({ref.epoch:%d %b %Y %H:%M}Z). SGP4 is not meaningful beyond "
            f"{MAX_WINDOW_DAYS} days, so this state cannot be placed against it.")
    r_ref, v_ref = propagate(ref.satrec(), sv.epoch)
    sign = -1.0 if invert else 1.0
    offset = sign * along_track_offset(to_teme(sv), r_ref, v_ref)
    return offset, float(np.linalg.norm(v_ref))


def _trace(uid: str, sv: StateVector, offset: float, label: str) -> dict:
    """A Plotly trace for one manual state, marked on the chart itself."""
    on_chart = f"MANUAL · {label}" if label else "MANUAL"
    hover = (f"<b>MANUAL STATE</b> · not from UDL · not saved"
             f"{'<br>' + esc(label) if label else ''}<br>{sv.frame}"
             "<br>%{y|%d %b %H:%M:%S}Z<br>offset %{x:.1f} s<extra></extra>")
    return {
        "type": "scatter",
        "mode": "markers+text",
        "uid": uid,
        "name": "manual",
        "x": [offset],
        "y": [sv.epoch.isoformat()],
        "showlegend": False,
        "cliponaxis": False,
        "marker": {"symbol": "star", "size": 18, "color": MANUAL_COLOUR,
                   "line": {"color": MANUAL_RING, "width": 2}},
        "text": [esc(on_chart)],
        # Towards zero. A manual state far from the datum is usually the
        # outermost point, so the axis ends at it and a label pointing outwards
        # is cut off by the plot edge; zero is always inside the plot.
        "textposition": "middle left" if offset >= 0 else "middle right",
        "textfont": {"color": MANUAL_RING, "size": 11},
        "hovertemplate": hover,
    }


def _row(uid: str, sv: StateVector, offset: float, km: float, label: str) -> str:
    """The rail entry for one manual state, with its remove control."""
    name = esc(label) if label else "Manual state"
    return (
        f'<li class="manrow" data-uid="{uid}">'
        '<div class="manhead"><span class="mantag">TEMPORARY</span>'
        f'<span class="manname">{name}</span>'
        '<button class="manx" type="button" aria-label="Remove this manual state">'
        "&times;</button></div>"
        f'<div class="manval">{offset:+.1f} s · ~{km:,.0f} km · '
        f"{sv.epoch:%d %b %H:%M:%S}Z · {sv.frame}</div></li>")


def plot_manual(body: dict) -> dict:
    """Validate one hand-entered state and place it against a reference.

    -> {"offset", "km", "epoch", "frame", "label", "trace", "row"}.
    """
    sv = StateVector(
        epoch=parse_epoch(body.get("epoch", "")),
        r=_vector(body, ("x", "y", "z"), "km"),
        v=_vector(body, ("vx", "vy", "vz"), "km/s"),
        frame=parse_frame(body.get("frame")),
        source="manual entry")
    check_state(sv.r, sv.v)
    label = parse_label(body.get("label"))
    ref = parse_reference(body.get("tle"))
    offset, speed = manual_offset(sv, ref, bool(body.get("invert")))
    km = abs(offset) * speed
    uid = "manual-" + uuid.uuid4().hex[:12]
    return {
        "offset": round(offset, 3),
        "km": round(km, 1),
        "epoch": sv.epoch.isoformat(),
        "frame": sv.frame,
        "label": label,
        "trace": _trace(uid, sv, offset, label),
        "row": _row(uid, sv, offset, km, label),
    }
