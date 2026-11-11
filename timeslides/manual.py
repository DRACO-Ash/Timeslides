"""A hand-entered state vector or element set, placed on a report's waterfall.

An operator sometimes holds a state that did not come from the UDL: from
another system, a manoeuvre plan, a colleague's solution. The question is
where it lands against what the UDL says. This module answers that one
question and nothing else.

Two kinds are accepted, and each is placed exactly as the report places its
UDL counterpart. A state vector is taken to TEME and projected at its epoch. An
element set is propagated to its own epoch and projected there, which is what
the report does for every element set in its element-set series, so a pasted
set is one point at its epoch rather than a track across the window.

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
KINDS = ("state", "elset")

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
# A floor rather than a test for zero: a float is compared against a bound,
# never for equality. A metre per second is far below any orbit.
MIN_SPEED_KMS = 1e-3

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
    if speed < MIN_SPEED_KMS:
        raise ValidationError(
            f"velocity magnitude is {speed:g} km/s, effectively zero; "
            "a state needs a velocity in km/s")


def tle_checksum(line: str) -> int:
    """The modulo-10 checksum of a TLE line: digits count as themselves, a
    minus sign as one, everything else as nothing."""
    return sum(int(c) if c.isdigit() else int(c == "-") for c in line[:68]) % 10


def _elset(line1: str, line2: str, what: str) -> Elset:
    """Shape-check and parse two TLE lines, taking the epoch from the set.

    The parser does not raise on a malformed set: it returns a satellite with
    a non-zero error and a meaningless epoch, so the error is what is checked.
    """
    if (len(line1) != TLE_LINE_LEN or len(line2) != TLE_LINE_LEN
            or not line1.startswith("1 ") or not line2.startswith("2 ")):
        raise ValidationError(
            f"{what} is not a two-line element set: two lines of {TLE_LINE_LEN} "
            "characters, starting 1 and 2")
    sat = Elset(epoch=_J2000_UTC, line1=line1, line2=line2).satrec()
    if sat.error:
        raise ValidationError(f"{what} could not be parsed")
    epoch = _J2000_UTC + dt.timedelta(days=sat.jdsatepoch + sat.jdsatepochF - _J2000_JD)
    return Elset(epoch=epoch, line1=line1, line2=line2, source="manual entry")


def parse_reference(raw) -> Elset:
    """The report's own reference element set, as the page sent it back.

    It comes from the report payload, but it crosses the HTTP boundary like
    anything else, so its shape is checked before the propagator sees it.
    """
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        raise ValidationError("the reference element set must be two TLE lines")
    line1, line2 = (str(x) for x in raw)
    return _elset(line1, line2, "the reference element set")


def parse_pasted_elset(raw) -> Elset:
    """An element set pasted by hand, as two lines or as three with a name.

    Stricter than the reference, because this one was typed or copied: the
    checksums are verified, since a single wrong digit in a pasted set moves
    the point without any other sign, and the two lines must belong to the
    same object. Trailing spaces and Windows line endings are tolerated; a
    name line above the set is accepted and not used.
    """
    lines = [line.rstrip() for line in str(raw or "").splitlines() if line.strip()]
    if len(lines) == 3 and not lines[0].startswith("1 "):
        lines = lines[1:]
    if len(lines) != 2:
        raise ValidationError(
            "paste the two lines of the element set; a name line above them is allowed")
    line1, line2 = lines
    els = _elset(line1, line2, "the element set")
    for n, line in ((1, line1), (2, line2)):
        want = tle_checksum(line)
        if line[68] != str(want):
            raise ValidationError(
                f"line {n} checksum is {line[68]} but the line sums to {want}: "
                "check the element set for a copying error")
    if line1[2:7] != line2[2:7]:
        raise ValidationError(
            f"line 1 is for object {line1[2:7].strip()} and line 2 for "
            f"{line2[2:7].strip()}; both lines must be from the same element set")
    return els


def offset_against(epoch: dt.datetime, r_teme: np.ndarray, ref: Elset,
                   invert: bool) -> tuple[float, float]:
    """(offset in seconds, reference speed in km/s) for a TEME position.

    Propagate the reference to the epoch and project onto its direction of
    travel: the step both kinds share, and the same one the report's series
    take for every point.
    """
    gap = abs((epoch - ref.epoch).total_seconds()) / 86400.0
    if gap > MAX_WINDOW_DAYS:
        raise ValidationError(
            f"epoch is {gap:,.0f} days from the reference element set "
            f"({ref.epoch:%d %b %Y %H:%M}Z). SGP4 is not meaningful beyond "
            f"{MAX_WINDOW_DAYS} days, so this cannot be placed against it.")
    r_ref, v_ref = propagate(ref.satrec(), epoch)
    sign = -1.0 if invert else 1.0
    offset = sign * along_track_offset(r_teme, r_ref, v_ref)
    return offset, float(np.linalg.norm(v_ref))


def manual_offset(sv: StateVector, ref: Elset, invert: bool) -> tuple[float, float]:
    """A state vector, as physics._state_offsets places one: to TEME, then
    projected at its own epoch."""
    return offset_against(sv.epoch, to_teme(sv), ref, invert)


def elset_offset(els: Elset, ref: Elset, invert: bool) -> tuple[float, float]:
    """An element set, as physics._tle_offsets places one: propagated to its
    own epoch, which gives a TEME position, then projected there."""
    r_obs, _v = propagate(els.satrec(), els.epoch)
    return offset_against(els.epoch, r_obs, ref, invert)


# How each kind is drawn and named. Filled means measured and open means
# derived, the plot's existing convention, so a pasted element set is the open
# form of the same star, just as the element-set series are open squares.
_LOOK = {
    "state": {"symbol": "star", "fill": MANUAL_COLOUR, "chart": "MANUAL",
              "hover": "MANUAL STATE", "tag": "STATE", "unnamed": "Manual state"},
    "elset": {"symbol": "star-open", "fill": MANUAL_RING, "chart": "MANUAL ELSET",
              "hover": "MANUAL ELEMENT SET", "tag": "ELSET", "unnamed": "Manual element set"},
}


def _trace(uid: str, kind: str, epoch: dt.datetime, offset: float, label: str,
           detail: str) -> dict:
    """A Plotly trace for one manual point, marked on the chart itself."""
    look = _LOOK[kind]
    on_chart = f"{look['chart']} · {label}" if label else look["chart"]
    hover = (f"<b>{look['hover']}</b> · not from UDL · not saved"
             f"{'<br>' + esc(label) if label else ''}<br>{esc(detail)}"
             "<br>%{y|%d %b %H:%M:%S}Z<br>offset %{x:.1f} s<extra></extra>")
    return {
        "type": "scatter",
        "mode": "markers+text",
        "uid": uid,
        "name": "manual",
        "x": [offset],
        "y": [epoch.isoformat()],
        "showlegend": False,
        "cliponaxis": False,
        "marker": {"symbol": look["symbol"], "size": 18, "color": look["fill"],
                   "line": {"color": MANUAL_RING, "width": 2}},
        "text": [esc(on_chart)],
        # Towards zero. A manual point far from the datum is usually the
        # outermost point, so the axis ends at it and a label pointing outwards
        # is cut off by the plot edge; zero is always inside the plot.
        "textposition": "middle left" if offset >= 0 else "middle right",
        "textfont": {"color": MANUAL_RING, "size": 11},
        "hovertemplate": hover,
    }


def _row(uid: str, kind: str, epoch: dt.datetime, offset: float, km: float,
         label: str, detail: str) -> str:
    """The rail entry for one manual point, with its remove control."""
    look = _LOOK[kind]
    name = esc(label) if label else look["unnamed"]
    return (
        f'<li class="manrow" data-uid="{uid}">'
        '<div class="manhead"><span class="mantag">TEMPORARY</span>'
        f'<span class="mankind">{look["tag"]}</span>'
        f'<span class="manname">{name}</span>'
        '<button class="manx" type="button" aria-label="Remove this manual entry">'
        "&times;</button></div>"
        f'<div class="manval">{offset:+.1f} s · ~{km:,.0f} km · '
        f"{epoch:%d %b %H:%M:%S}Z · {esc(detail)}</div></li>")


def parse_kind(raw) -> str:
    kind = str(raw or "state").strip().lower()
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    return kind


def _observe(kind: str, body: dict, ref: Elset, invert: bool) -> tuple:
    """-> (epoch, offset, reference speed, detail) for either kind."""
    if kind == "elset":
        els = parse_pasted_elset(body.get("elset"))
        offset, speed = elset_offset(els, ref, invert)
        return els.epoch, offset, speed, f"NORAD {els.line1[2:7].strip()} · TEME"
    sv = StateVector(
        epoch=parse_epoch(body.get("epoch", "")),
        r=_vector(body, ("x", "y", "z"), "km"),
        v=_vector(body, ("vx", "vy", "vz"), "km/s"),
        frame=parse_frame(body.get("frame")),
        source="manual entry")
    check_state(sv.r, sv.v)
    offset, speed = manual_offset(sv, ref, invert)
    return sv.epoch, offset, speed, sv.frame


def plot_manual(body: dict) -> dict:
    """Validate one hand-entered state vector or element set and place it
    against a reference.

    -> {"kind", "offset", "km", "epoch", "detail", "label", "trace", "row"}.
    """
    kind = parse_kind(body.get("kind"))
    label = parse_label(body.get("label"))
    ref = parse_reference(body.get("tle"))
    epoch, offset, speed, detail = _observe(kind, body, ref, bool(body.get("invert")))
    km = abs(offset) * speed
    uid = "manual-" + uuid.uuid4().hex[:12]
    return {
        "kind": kind,
        "offset": round(offset, 3),
        "km": round(km, 1),
        "epoch": epoch.isoformat(),
        "detail": detail,
        "label": label,
        "trace": _trace(uid, kind, epoch, offset, label, detail),
        "row": _row(uid, kind, epoch, offset, km, label, detail),
    }
