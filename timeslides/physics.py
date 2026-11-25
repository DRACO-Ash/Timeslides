"""Phase-offset maths.

The method is unchanged from LEO_Waterfall_Phase_Offset.py:

  1. Propagate the REFERENCE TLE to t  -> r_ref, v_ref   (SGP4, TEME frame)
     From 1.10 "the reference TLE" is the pair of reference element sets
     either side of t, and steps 3 are blended by time between them: see
     Datum below and AUDIT.md, "The rolling datum".
  2. Get the measured position at t    -> r_obs
  3. dr = r_obs - r_ref
     t_hat = v_ref / |v_ref|
     phase_offset = (dr . t_hat) / |v_ref|   seconds, signed

What has changed is that the per-point loops are now batched, because the
per-point form cannot serve an HTTP request. Measured on this toolchain:

  * astropy GCRS to TEME, one vector at a time: 33 ms each. A run of seven
    objects across three providers can carry order 10^5 state vectors, which
    is roughly 58 minutes of frame conversion. Batched: 0.2 ms each, about
    21 seconds. Bit-identical, max absolute difference 0.0 km.
  * sgp4_array against repeated Satrec.sgp4: 5x faster, bit-identical
    positions and velocities.

Both of those are exactly equal to the original, and the tests assert exact
equality. The one place the batch form is not bit-identical is the along-track
projection, where einsum sums in a different order from np.dot: measured worst
case 1.1e-13 seconds absolute, 1.7e-14 relative, which is the last bit or two
of a double. Stating that plainly rather than claiming exactness. It is around
ten orders of magnitude below the uncertainty of the underlying radar and
element-set data, so it cannot affect a reading of the plot, and the tests
hold it to a tolerance rather than pretending it is zero.

The scalar helpers are kept, exported and tested, and the batch helpers are
asserted equal to them in tests/test_physics.py, so none of this is taken
on trust.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from sgp4.api import Satrec, jday

from .errors import ComputeError, ConfigError
from .models import ELSET_KEY, StateVector


# --------------------------------------------------------------------------- #
#  Frame handling
# --------------------------------------------------------------------------- #
def _astropy():
    """Import astropy lazily and turn a missing install into a clear boot-time
    failure rather than a mid-request traceback. Demo mode is TEME throughout
    and never reaches here."""
    try:
        from astropy import units as u
        from astropy.coordinates import GCRS, TEME, CartesianRepresentation
        from astropy.time import Time
    except ImportError as exc:  # pragma: no cover - exercised by env, not tests
        raise ConfigError(
            "State vectors in a non-TEME frame need astropy to convert to TEME. "
            "Install astropy, or request TEME from the UDL."
        ) from exc
    # Returned under lowercase names: these are locals, and a local spelled
    # like a class trips the naming rule without making anything clearer.
    return u, Time, GCRS, TEME, CartesianRepresentation


class _Astropy:
    """The handful of astropy names this module uses, bound once."""

    __slots__ = ("cartesian", "gcrs", "teme", "time", "units")

    def __init__(self, units, time, gcrs, teme, cartesian):
        self.units = units
        self.time = time
        self.gcrs = gcrs
        self.teme = teme
        self.cartesian = cartesian


def _gcrs_to_teme(epochs: list, rows: np.ndarray) -> np.ndarray:
    """Convert an (N, 3) block of J2000/GCRF positions in km to TEME in km.

    J2000/GCRF is treated as GCRS, exactly as the original single-vector path
    did. One astropy call for the whole block.
    """
    ap = _Astropy(*_astropy())
    when = ap.time(list(epochs), scale="utc")
    src = ap.gcrs(ap.cartesian((rows * ap.units.km).T), obstime=when)
    out = src.transform_to(ap.teme(obstime=when)).cartesian.xyz.to(ap.units.km).value
    return np.atleast_2d(out.T)


def to_teme(sv: StateVector) -> np.ndarray:
    """Return one state vector's position in TEME (km)."""
    frame = (sv.frame or "TEME").upper()
    if frame in ("TEME", ""):
        return sv.r
    return _gcrs_to_teme([sv.epoch], np.asarray(sv.r, dtype=float)[None, :])[0]


def to_teme_batch(svs: list) -> np.ndarray:
    """Return an (N, 3) block of TEME positions in km for `svs`, in order.

    State vectors are grouped by declared frame so that each frame costs one
    astropy call. In practice a series carries one frame, but records are
    allowed to disagree and this handles that without falling back to the slow
    path for the whole series.
    """
    if not svs:
        return np.empty((0, 3))
    out = np.empty((len(svs), 3), dtype=float)
    by_frame: dict = {}
    for i, sv in enumerate(svs):
        by_frame.setdefault((sv.frame or "TEME").upper(), []).append(i)
    for frame, idx in by_frame.items():
        rows = np.array([np.asarray(svs[i].r, dtype=float) for i in idx])
        if frame in ("TEME", ""):
            out[idx] = rows
        else:
            out[idx] = _gcrs_to_teme([svs[i].epoch for i in idx], rows)
    return out


# --------------------------------------------------------------------------- #
#  Propagation
# --------------------------------------------------------------------------- #
def _jday(when: dt.datetime) -> tuple[float, float]:
    return jday(when.year, when.month, when.day,
                when.hour, when.minute, when.second + when.microsecond * 1e-6)


def propagate(sat: Satrec, when: dt.datetime) -> tuple[np.ndarray, np.ndarray]:
    """Propagate an SGP4 satellite to a UTC datetime -> (r, v) in TEME, km / km/s."""
    jd, fr = _jday(when)
    e, r, v = sat.sgp4(jd, fr)
    if e != 0:
        raise ComputeError(f"SGP4 error code {e} at {when.isoformat()}")
    return np.array(r), np.array(v)


def propagate_batch(sat: Satrec, whens: list) -> tuple[np.ndarray, np.ndarray]:
    """Propagate one satellite to many epochs -> ((N,3) r, (N,3) v) in TEME."""
    if not whens:
        return np.empty((0, 3)), np.empty((0, 3))
    jds = np.empty(len(whens))
    frs = np.empty(len(whens))
    for i, when in enumerate(whens):
        jds[i], frs[i] = _jday(when)
    errs, r, v = sat.sgp4_array(jds, frs)
    bad = np.nonzero(errs)[0]
    if bad.size:
        i = int(bad[0])
        raise ComputeError(f"SGP4 error code {int(errs[i])} at {whens[i].isoformat()}")
    return r, v


# --------------------------------------------------------------------------- #
#  Along-track offset
# --------------------------------------------------------------------------- #
def along_track_offset(r_obs: np.ndarray, r_ref: np.ndarray, v_ref: np.ndarray) -> float:
    """Signed along-track timing offset in seconds. + ahead of reference, - behind."""
    speed = np.linalg.norm(v_ref)
    t_hat = v_ref / speed
    along_km = float(np.dot(r_obs - r_ref, t_hat))
    return along_km / speed


def along_track_offsets(r_obs: np.ndarray, r_ref: np.ndarray,
                        v_ref: np.ndarray) -> np.ndarray:
    """Vectorised form of along_track_offset over (N, 3) blocks -> (N,) seconds.

    Kept in the same algebraic order as the scalar version (normalise, project,
    divide) rather than the shorter dot(dr, v)/speed**2, so the two agree to
    the bit.
    """
    speed = np.linalg.norm(v_ref, axis=1)
    t_hat = v_ref / speed[:, None]
    along_km = np.einsum("ij,ij->i", r_obs - r_ref, t_hat)
    return along_km / speed


# --------------------------------------------------------------------------- #
#  Reference orbit and series assembly
# --------------------------------------------------------------------------- #
def reference_elset(objects, ref_sat_no, ref_epoch):
    """The reference's single best element set: the whole datum under
    "Latest only", and the datum beyond the newest epoch under Rolling, which
    is where the Look-ahead forecasts. Before 1.10 it was the only datum.

    Default: the LATEST element set of the reference object, or the one nearest
    ref_epoch when an epoch is asked for.

    Latest, not earliest. The reference is the datum every offset on the plot
    is measured from, so it should be the best current knowledge of where the
    reference object is. Anchoring on the earliest element set in the window
    means propagating the oldest orbit forward across the whole window, and
    SGP4 error grows with time since epoch, so the further right the plot goes
    the more of the drift belongs to the reference rather than to the object
    being measured. Every series then carries that error in common, which
    looks like real relative motion and is not.

    Returns the element set rather than its satrec because the report carries
    the datum itself, so that a hand-entered state can be measured against
    exactly what the plot was. See timeslides/manual.py.
    """
    ref_obj = next((o for o in objects if o.sat_no == ref_sat_no), None)
    if ref_obj is None:
        raise ComputeError(f"reference sat {ref_sat_no} is not among the loaded objects")
    if not ref_obj.elsets:
        raise ComputeError(
            f"reference sat {ref_sat_no} has no TLEs to anchor the reference orbit")
    ref_obj.elsets.sort(key=lambda e: e.epoch)
    if ref_epoch is None:
        ref = ref_obj.elsets[-1]   # sorted ascending above, so this is latest
    else:
        ref = min(ref_obj.elsets, key=lambda e: abs((e.epoch - ref_epoch).total_seconds()))
    return ref


class Datum:
    """What every offset in a dataset is measured from.

    A chain of the reference object's element sets, sorted by epoch. A point
    at time t is offset against the element set at or before t and the one
    after it, and the two offsets are blended linearly by where t falls
    between their epochs. Before the first epoch the first element set is
    used alone; after the last, the last alone, which is also what the
    Look-ahead forecasts from.

    Why a chain and not one element set: SGP4 error grows with time from an
    element set's epoch. One element set stretched across the window puts
    that error on every series at once, where it reads as relative motion.
    Measured on a synthetic truth (see AUDIT.md, "The rolling datum"), the
    rolling form cut the RMS error of a 7-day window from 0.21 to 1.33 s down
    to 0.03 s, and of a 90-day window from about 280 s to about 0.2 s.

    A chain of ONE element set is exactly the datum Timeslides used before:
    every point takes the same branch, through the same propagation and the
    same projection, so "latest only" is reproduced bit for bit rather than
    reimplemented. tests/test_datum.py holds it to that.
    """

    def __init__(self, elsets: list):
        if not elsets:
            raise ComputeError("a datum needs at least one element set")
        ordered = sorted(elsets, key=lambda e: e.epoch)
        chain = []
        for els in ordered:
            # Two element sets at one epoch (two originators) cannot both be
            # bracket ends: the blend weight would divide by zero. The first
            # in epoch order is kept, which is the UDL's own order.
            if chain and els.epoch == chain[-1].epoch:
                continue
            chain.append(els)
        self.elsets = chain
        self.epochs = [e.epoch for e in chain]
        self.sats = [e.satrec() for e in chain]
        self._t0 = self.epochs[0]
        self._secs = np.array([(e - self._t0).total_seconds() for e in self.epochs])

    @classmethod
    def single(cls, sat: Satrec) -> Datum:
        """A one-element datum from a bare satrec, for callers that hold only
        that: the old interface, kept so nothing that used it has to change."""
        datum = cls.__new__(cls)
        datum.elsets, datum.epochs, datum.sats = [], [None], [sat]
        datum._t0, datum._secs = None, np.zeros(1)
        return datum

    @property
    def rolling(self) -> bool:
        return len(self.sats) > 1

    def longest_gap_hours(self) -> float:
        return float(np.max(np.diff(self._secs)) / 3600.0) if self.rolling else 0.0

    def _segments(self, epochs: list) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(a, b, w) per point: bracket indices and the weight of b."""
        n = len(self.sats)
        if n == 1:
            zero = np.zeros(len(epochs), dtype=int)
            return zero, zero, np.zeros(len(epochs))
        t = np.array([(e - self._t0).total_seconds() for e in epochs])
        a = np.searchsorted(self._secs, t, side="right") - 1
        a = np.clip(a, 0, n - 1)
        b = np.minimum(a + 1, n - 1)
        span = self._secs[b] - self._secs[a]
        w = np.where(span > 0, (t - self._secs[a]) / np.where(span > 0, span, 1.0), 0.0)
        w = np.clip(w, 0.0, 1.0)     # before the first epoch: the first alone
        return a, b, w

    def offsets(self, epochs: list, r_obs: np.ndarray, sign: float) -> np.ndarray:
        """Signed along-track offsets, in seconds, for positions at epochs."""
        a, b, w = self._segments(epochs)
        out = np.empty(len(epochs))
        for seg in np.unique(a):
            idx = np.nonzero(a == seg)[0]
            when = [epochs[i] for i in idx]
            r_a, v_a = propagate_batch(self.sats[seg], when)
            off = along_track_offsets(r_obs[idx], r_a, v_a)
            nxt = int(b[idx[0]])
            if nxt != seg:
                r_b, v_b = propagate_batch(self.sats[nxt], when)
                off = (1.0 - w[idx]) * off + w[idx] * along_track_offsets(r_obs[idx], r_b, v_b)
            out[idx] = sign * off
        return out

    def speed_at(self, epoch: dt.datetime) -> float:
        """The reference's speed at an epoch, from the element set at or
        before it, for turning seconds into kilometres."""
        a, _b, _w = self._segments([epoch])
        _r, v = propagate(self.sats[int(a[0])], epoch)
        return float(np.linalg.norm(v))


def as_datum(datum) -> Datum:
    return datum if isinstance(datum, Datum) else Datum.single(datum)


def reference_chain(objects, ref_sat_no) -> list:
    """Every element set of the reference object, for a rolling datum."""
    ref_obj = next((o for o in objects if o.sat_no == ref_sat_no), None)
    if ref_obj is None:
        raise ComputeError(f"reference sat {ref_sat_no} is not among the loaded objects")
    if not ref_obj.elsets:
        raise ComputeError(
            f"reference sat {ref_sat_no} has no TLEs to anchor the reference orbit")
    return list(ref_obj.elsets)


def _state_offsets(svs: list, datum, sign: float) -> list:
    """One state-vector series against the datum.

    -> [(epoch, seconds, source)]. The source travels with the point rather
    than being reconstructed alongside it, because two lists that have to stay
    in the same order are two chances to get the order wrong.
    """
    ordered = sorted(svs, key=lambda s: s.epoch)
    epochs = [s.epoch for s in ordered]
    offsets = as_datum(datum).offsets(epochs, to_teme_batch(ordered), sign).tolist()
    return [(sv.epoch, off, sv.source)
            for sv, off in zip(ordered, offsets, strict=True)]


def _tle_offsets(elsets: list, datum, sign: float) -> list:
    """The TLE series against the datum.

    -> [(epoch, seconds, source)]. Each element set carries its own orbit, so
    this is one SGP4 call per point for the observed side and batched calls
    for the datum side.

    The source matters more here than anywhere else on the plot. Unlike the
    state-vector series, this one is not filtered by provider: whatever the
    tenant holds on /udl/elset comes back, so a point's originator is a
    property of the point and not of the series it sits in.
    """
    ordered = sorted(elsets, key=lambda e: e.epoch)
    if not ordered:
        return []
    epochs = [e.epoch for e in ordered]
    r_obs = np.array([propagate(e.satrec(), e.epoch)[0] for e in ordered])
    offsets = as_datum(datum).offsets(epochs, r_obs, sign).tolist()
    return [(e.epoch, off, e.source)
            for e, off in zip(ordered, offsets, strict=True)]


def compute_series(obj, datum, invert: bool) -> dict:
    """Offset every state source and the TLEs of `obj` against the shared
    datum, a Datum or a bare satrec (a one-element datum). Returns {source_key: [(epoch, offset_s, source), ...]},
    one entry per state-vector provider present on the object plus the
    element-set series under ELSET_KEY.

    `source` is the record's own account of its originator, which may be empty
    where the feed did not give one. It is per point rather than per series
    because the element-set query is not filtered by provider.
    """
    sign = -1.0 if invert else 1.0
    datum = as_datum(datum)
    out = {key: _state_offsets(svs, datum, sign)
           for key, svs in obj.state_series.items()}
    out[ELSET_KEY] = _tle_offsets(obj.elsets, datum, sign)
    return out
