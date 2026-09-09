"""The batch maths must equal the original per-point maths.

Phase 1 replaced the per-point loops in compute_series with batched astropy and
sgp4 calls, for a roughly 160x speed-up on frame conversion. That is only a safe
change if it is numerically the same change, so the original implementation is
reproduced here verbatim as `_scalar_compute_series` and the batched code is
asserted equal to it. If someone later 'optimises' the algebra into a form that
drifts, these tests fail.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from timeslides import physics
from timeslides.demo import build_demo, build_demo_modes
from timeslides.errors import ComputeError
from timeslides.models import Elset, StateVector

START = dt.datetime(2026, 6, 24)
END = dt.datetime(2026, 7, 1)


# --------------------------------------------------------------------------- #
#  The original implementation, kept as the oracle
# --------------------------------------------------------------------------- #
def _scalar_to_teme(sv):
    frame = (sv.frame or "TEME").upper()
    if frame in ("TEME", ""):
        return sv.r
    from astropy import units as u
    from astropy.coordinates import GCRS, TEME, CartesianRepresentation
    from astropy.time import Time
    t = Time(sv.epoch, scale="utc")
    src = GCRS(CartesianRepresentation(sv.r * u.km), obstime=t)
    return src.transform_to(TEME(obstime=t)).cartesian.xyz.to(u.km).value


def _scalar_offset(r_obs, r_ref, v_ref):
    speed = np.linalg.norm(v_ref)
    t_hat = v_ref / speed
    return float(np.dot(r_obs - r_ref, t_hat)) / speed


def _scalar_compute_series(obj, ref_sat, invert):
    sign = -1.0 if invert else 1.0
    out = {}
    for key, svs in obj.state_series.items():
        series = []
        for sv in sorted(svs, key=lambda s: s.epoch):
            r_ref, v_ref = physics.propagate(ref_sat, sv.epoch)
            series.append((sv.epoch, sign * _scalar_offset(_scalar_to_teme(sv), r_ref, v_ref)))
        out[key] = series
    tle = []
    for els in sorted(obj.elsets, key=lambda e: e.epoch):
        r_ref, v_ref = physics.propagate(ref_sat, els.epoch)
        r_obs, _ = physics.propagate(els.satrec(), els.epoch)
        tle.append((els.epoch, sign * _scalar_offset(r_obs, r_ref, v_ref)))
    out["spacetrack"] = tle
    return out


@pytest.fixture(scope="module")
def demo_objects():
    return build_demo(START, END)


@pytest.fixture(scope="module")
def ref_sat(demo_objects):
    return physics.reference_satrec(demo_objects, 59884, None)


# --------------------------------------------------------------------------- #
#  Equivalence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("invert", [False, True])
def test_compute_series_matches_original_scalar_implementation(
        demo_objects, ref_sat, invert):
    for obj in demo_objects:
        got = physics.compute_series(obj, ref_sat, invert)
        want = _scalar_compute_series(obj, ref_sat, invert)
        assert set(got) == set(want)
        for key in want:
            assert [e for e, _ in got[key]] == [e for e, _ in want[key]]
            # Not asserted exactly equal: einsum sums the projection in a
            # different order from np.dot, worth about 1e-13 seconds. See the
            # module docstring in timeslides/physics.py. The tolerance is still
            # ten orders of magnitude tighter than the data warrants.
            np.testing.assert_allclose(
                [v for _, v in got[key]], [v for _, v in want[key]],
                rtol=1e-11, atol=1e-9,
                err_msg=f"series {key} diverged from the original implementation")


def test_offsets_are_plain_python_floats(demo_objects, ref_sat):
    """The renderer serialises these with json.dumps, which rejects numpy
    scalars. A numpy float here would fail only at render time."""
    series = physics.compute_series(demo_objects[0], ref_sat, False)
    for points in series.values():
        for _, value in points:
            assert type(value) is float


def test_batch_propagation_matches_scalar(demo_objects, ref_sat):
    epochs = [sv.epoch for sv in demo_objects[0].state_series["leolabs"]]
    r_b, v_b = physics.propagate_batch(ref_sat, epochs)
    for i, epoch in enumerate(epochs):
        r_s, v_s = physics.propagate(ref_sat, epoch)
        np.testing.assert_array_equal(r_b[i], r_s)
        np.testing.assert_array_equal(v_b[i], v_s)


def test_batch_offsets_match_scalar():
    rng = np.random.default_rng(3)
    r_obs, r_ref = rng.normal(0, 7000, (40, 3)), rng.normal(0, 7000, (40, 3))
    v_ref = rng.normal(0, 7.5, (40, 3))
    batch = physics.along_track_offsets(r_obs, r_ref, v_ref)
    scalar = [physics.along_track_offset(r_obs[i], r_ref[i], v_ref[i]) for i in range(40)]
    np.testing.assert_allclose(batch, scalar, rtol=1e-11, atol=1e-9)


# --------------------------------------------------------------------------- #
#  Frame conversion
# --------------------------------------------------------------------------- #
def test_teme_states_pass_through_untouched():
    sv = StateVector(epoch=START, r=np.array([1.0, 2.0, 3.0]),
                     v=np.array([4.0, 5.0, 6.0]), frame="TEME")
    np.testing.assert_array_equal(physics.to_teme(sv), sv.r)
    np.testing.assert_array_equal(physics.to_teme_batch([sv])[0], sv.r)


def test_blank_frame_is_treated_as_teme():
    sv = StateVector(epoch=START, r=np.array([1.0, 2.0, 3.0]),
                     v=np.zeros(3), frame="")
    np.testing.assert_array_equal(physics.to_teme(sv), sv.r)


def test_j2000_batch_conversion_matches_scalar():
    """The astropy path, batched against one-at-a-time."""
    rng = np.random.default_rng(11)
    svs = [StateVector(epoch=START + dt.timedelta(minutes=13 * i),
                       r=rng.normal(0, 7000, 3), v=rng.normal(0, 7.5, 3),
                       frame="J2000")
           for i in range(12)]
    batch = physics.to_teme_batch(svs)
    for i, sv in enumerate(svs):
        np.testing.assert_array_equal(batch[i], _scalar_to_teme(sv))
        np.testing.assert_array_equal(batch[i], physics.to_teme(sv))


def test_mixed_frames_in_one_series_are_each_converted_correctly():
    """Records are allowed to disagree about their frame. Grouping must not
    leak one frame's handling onto another's rows."""
    rng = np.random.default_rng(5)
    svs = []
    for i in range(8):
        frame = "TEME" if i % 2 else "J2000"
        svs.append(StateVector(epoch=START + dt.timedelta(minutes=9 * i),
                               r=rng.normal(0, 7000, 3), v=rng.normal(0, 7.5, 3),
                               frame=frame))
    batch = physics.to_teme_batch(svs)
    for i, sv in enumerate(svs):
        np.testing.assert_array_equal(batch[i], _scalar_to_teme(sv))


def test_empty_inputs_do_not_explode():
    assert physics.to_teme_batch([]).shape == (0, 3)
    r, v = physics.propagate_batch(None, [])
    assert r.shape == (0, 3) and v.shape == (0, 3)


# --------------------------------------------------------------------------- #
#  Reference orbit selection and failure modes
# --------------------------------------------------------------------------- #
def test_reference_orbit_defaults_to_earliest_tle(demo_objects):
    obj = next(o for o in demo_objects if o.sat_no == 59884)
    earliest = min(e.epoch for e in obj.elsets)
    sat = physics.reference_satrec(demo_objects, 59884, None)
    expected = next(e for e in obj.elsets if e.epoch == earliest).satrec()
    assert sat.jdsatepoch == expected.jdsatepoch


def test_reference_orbit_honours_a_requested_epoch(demo_objects):
    obj = next(o for o in demo_objects if o.sat_no == 59884)
    target = sorted(e.epoch for e in obj.elsets)[3]
    sat = physics.reference_satrec(demo_objects, 59884, target)
    expected = next(e for e in obj.elsets if e.epoch == target).satrec()
    assert sat.jdsatepoch == expected.jdsatepoch


def test_unknown_reference_object_raises_compute_error(demo_objects):
    with pytest.raises(ComputeError, match="not among the loaded objects"):
        physics.reference_satrec(demo_objects, 12345, None)


def test_reference_object_without_tles_raises_compute_error(demo_objects):
    obj = next(o for o in demo_objects if o.sat_no == 59884)
    stripped = [o for o in demo_objects if o.sat_no != 59884]
    bare = type(obj)(sat_no=59884, name=obj.name, colour=obj.colour)
    with pytest.raises(ComputeError, match="no TLEs to anchor"):
        physics.reference_satrec(stripped + [bare], 59884, None)


def test_sgp4_failure_surfaces_as_compute_error_not_systemexit():
    """A decayed or otherwise unpropagatable orbit must return a status code,
    not take the worker process down."""
    # High drag term and a mean motion near re-entry: SGP4 returns error 1
    # (mean eccentricity out of range) a few days past epoch. Deterministic.
    bad = Elset(epoch=START,
                line1="1 25544U 98067A   26175.50000000  .00016717  00000-0  99999-1 0  9005",
                line2="2 25544  51.6400 208.9163 0006317  69.9862 290.1789 16.49309620 10005")
    sat = bad.satrec()
    far = START + dt.timedelta(days=20)
    with pytest.raises(ComputeError, match="SGP4 error code"):
        physics.propagate(sat, far)
    with pytest.raises(ComputeError, match="SGP4 error code"):
        physics.propagate_batch(sat, [far])


def test_reference_object_sits_near_zero_against_itself(demo_objects, ref_sat):
    """The physical sanity check: the waterfall anchor compared with itself
    should sit near zero, and a drifting object should not."""
    ref = next(o for o in demo_objects if o.sat_no == 59884)
    other = next(o for o in demo_objects if o.sat_no == 67689)
    ref_series = physics.compute_series(ref, ref_sat, False)
    other_series = physics.compute_series(other, ref_sat, False)
    for key in ("leolabs", "northstar"):
        assert max(abs(v) for _, v in ref_series[key]) < 0.5, key
        assert max(abs(v) for _, v in other_series[key]) > 50.0, key


def test_demo_tle_series_drifts_because_of_the_tle_text_round_trip(demo_objects, ref_sat):
    """Documents a property of the demo generator that otherwise reads as a bug.

    The demo builds each element set by exporting a Satrec to TLE text. The
    two-line format carries mean motion to eight decimal places, so the
    exported and re-imported orbit differs from the one that produced it by a
    hair. Over a seven-day window that truncation integrates into tens of
    seconds of along-track offset, so the reference object's own TLE series
    drifts away from zero while its state-vector series, which never go
    through TLE text, stay under 20 milliseconds.

    This is the synthetic data, not the maths. Live element sets come from the
    UDL as text and never make this round trip.
    """
    ref = next(o for o in demo_objects if o.sat_no == 59884)
    series = physics.compute_series(ref, ref_sat, False)
    assert max(abs(v) for _, v in series["spacetrack"]) > 1.0
    assert max(abs(v) for _, v in series["leolabs"]) < 0.5


def test_inverting_the_sign_negates_every_point(demo_objects, ref_sat):
    obj = demo_objects[1]
    plain = physics.compute_series(obj, ref_sat, False)
    flipped = physics.compute_series(obj, ref_sat, True)
    for key in plain:
        for (e1, v1), (e2, v2) in zip(plain[key], flipped[key]):
            assert e1 == e2
            assert v1 == -v2


def test_demo_modes_build_two_groups_with_both_data_modes():
    groups = build_demo_modes(START, END)
    assert [g["name"] for g in groups] == ["PRC Spaceplane", "LEO Cluster"]
    for g in groups:
        assert set(g["objects_by_mode"]) == {"REAL", "SIM"}
        assert g["reference"] in g["sat_order"]
