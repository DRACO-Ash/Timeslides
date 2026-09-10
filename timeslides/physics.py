"""Phase-offset maths.

The method is unchanged from LEO_Waterfall_Phase_Offset.py:

  1. Propagate the REFERENCE TLE to t  -> r_ref, v_ref   (SGP4, TEME frame)
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
def reference_satrec(objects, ref_sat_no, ref_epoch):
    """Build the single common reference orbit used for the whole waterfall.
    Default: earliest TLE of the reference object; or nearest to ref_epoch."""
    ref_obj = next((o for o in objects if o.sat_no == ref_sat_no), None)
    if ref_obj is None:
        raise ComputeError(f"reference sat {ref_sat_no} is not among the loaded objects")
    if not ref_obj.elsets:
        raise ComputeError(
            f"reference sat {ref_sat_no} has no TLEs to anchor the reference orbit")
    ref_obj.elsets.sort(key=lambda e: e.epoch)
    if ref_epoch is None:
        ref = ref_obj.elsets[0]
    else:
        ref = min(ref_obj.elsets, key=lambda e: abs((e.epoch - ref_epoch).total_seconds()))
    return ref.satrec()


def _state_offsets(svs: list, ref_sat: Satrec, sign: float) -> list:
    """One state-vector series against the reference orbit.

    -> [(epoch, seconds, source)]. The source travels with the point rather
    than being reconstructed alongside it, because two lists that have to stay
    in the same order are two chances to get the order wrong.
    """
    ordered = sorted(svs, key=lambda s: s.epoch)
    epochs = [s.epoch for s in ordered]
    r_ref, v_ref = propagate_batch(ref_sat, epochs)
    r_obs = to_teme_batch(ordered)
    offsets = (sign * along_track_offsets(r_obs, r_ref, v_ref)).tolist()
    return [(sv.epoch, off, sv.source)
            for sv, off in zip(ordered, offsets, strict=True)]


def _tle_offsets(elsets: list, ref_sat: Satrec, sign: float) -> list:
    """The TLE series against the reference orbit.

    -> [(epoch, seconds, source)]. Each element set carries its own orbit, so
    this is one SGP4 call per point for the observed side and one batched call
    for the reference side.

    The source matters more here than anywhere else on the plot. Unlike the
    state-vector series, this one is not filtered by provider: whatever the
    tenant holds on /udl/elset comes back, so a point's originator is a
    property of the point and not of the series it sits in.
    """
    ordered = sorted(elsets, key=lambda e: e.epoch)
    if not ordered:
        return []
    epochs = [e.epoch for e in ordered]
    r_ref, v_ref = propagate_batch(ref_sat, epochs)
    r_obs = np.array([propagate(e.satrec(), e.epoch)[0] for e in ordered])
    offsets = (sign * along_track_offsets(r_obs, r_ref, v_ref)).tolist()
    return [(e.epoch, off, e.source)
            for e, off in zip(ordered, offsets, strict=True)]


def compute_series(obj, ref_sat, invert: bool) -> dict:
    """Offset every state source and the TLEs of `obj` against the shared
    reference orbit. Returns {source_key: [(epoch, offset_s, source), ...]},
    one entry per state-vector provider present on the object plus the
    element-set series under ELSET_KEY.

    `source` is the record's own account of its originator, which may be empty
    where the feed did not give one. It is per point rather than per series
    because the element-set query is not filtered by provider.
    """
    sign = -1.0 if invert else 1.0
    out = {key: _state_offsets(svs, ref_sat, sign)
           for key, svs in obj.state_series.items()}
    out[ELSET_KEY] = _tle_offsets(obj.elsets, ref_sat, sign)
    return out
