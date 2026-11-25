"""The rolling datum: what every offset is measured from.

physics.Datum replaced one element set stretched across the window with a
chain of the reference's element sets, blended by time. These hold it to
three promises:

  * a chain of one element set IS the old datum, to the bit, so "Latest only"
    reproduces every earlier report rather than approximating it;
  * the blend does what it says at the ends, in the middle and outside the
    chain;
  * the report carries both readings, and the check that compares them uses
    only measurements that are independent of the datum.

Why the change was made, and the experiment that measured it, are in
AUDIT.md under "The rolling datum".
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from timeslides import physics
from timeslides.errors import ComputeError, ValidationError
from timeslides.models import ELSET_KEY

REF = 59884   # the demo's first reference object


@pytest.fixture(scope="module")
def demo_objects():
    from timeslides.demo import build_demo
    start = dt.datetime(2026, 6, 24)
    return build_demo(start, start + dt.timedelta(days=7))


@pytest.fixture(scope="module")
def ref_obj(demo_objects):
    return next(o for o in demo_objects if o.sat_no == REF)


def _old_offsets(sat, epochs, r_obs, sign):
    """The datum as it was before the chain: one satrec, one projection."""
    r_ref, v_ref = physics.propagate_batch(sat, epochs)
    return sign * physics.along_track_offsets(r_obs, r_ref, v_ref)


# --------------------------------------------------------------------------- #
#  One element set is the old datum, exactly
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_a_chain_of_one_is_bit_identical_to_the_old_datum(demo_objects, ref_obj, sign):
    latest = physics.reference_elset(demo_objects, REF, None)
    other = next(o for o in demo_objects if o.sat_no != REF and o.state_series)
    svs = sorted(next(iter(other.state_series.values())), key=lambda s: s.epoch)
    epochs = [s.epoch for s in svs]
    r_obs = physics.to_teme_batch(svs)
    want = _old_offsets(latest.satrec(), epochs, r_obs, sign)
    for datum in (physics.Datum([latest]), physics.Datum.single(latest.satrec())):
        got = datum.offsets(epochs, r_obs, sign)
        assert np.array_equal(got, want), "a one-element datum must match the old one exactly"


def test_compute_series_still_accepts_a_bare_satrec(demo_objects, ref_obj):
    """docker/build-rootfs.sh calls it that way, and so may anything else."""
    sat = physics.reference_elset(demo_objects, REF, None).satrec()
    via_sat = physics.compute_series(ref_obj, sat, False)
    via_datum = physics.compute_series(ref_obj, physics.Datum.single(sat), False)
    assert via_sat == via_datum


# --------------------------------------------------------------------------- #
#  The blend
# --------------------------------------------------------------------------- #
def test_the_reference_s_own_element_sets_sit_exactly_on_a_rolling_datum(ref_obj):
    datum = physics.Datum(physics.reference_chain([ref_obj], REF))
    series = physics.compute_series(ref_obj, datum, False)[ELSET_KEY]
    assert series, "the demo reference has element sets"
    assert all(off == 0.0 for _e, off, _s in series)


def test_at_an_epoch_the_blend_is_that_element_set_alone(ref_obj):
    chain = physics.Datum(ref_obj.elsets)
    i = len(chain.sats) // 2
    when = chain.epochs[i]
    r_obs = np.array([physics.propagate(chain.sats[i + 1], when)[0]])
    got = chain.offsets([when], r_obs, 1.0)[0]
    want = _old_offsets(chain.sats[i], [when], r_obs, 1.0)[0]
    assert got == pytest.approx(want, abs=1e-12)


def test_between_two_epochs_the_offsets_are_weighted_by_time(ref_obj):
    chain = physics.Datum(ref_obj.elsets)
    a, b = chain.epochs[1], chain.epochs[2]
    when = a + (b - a) / 4
    r_obs = np.array([physics.propagate(chain.sats[0], when)[0]])
    off_a = _old_offsets(chain.sats[1], [when], r_obs, 1.0)[0]
    off_b = _old_offsets(chain.sats[2], [when], r_obs, 1.0)[0]
    got = chain.offsets([when], r_obs, 1.0)[0]
    assert got == pytest.approx(0.75 * off_a + 0.25 * off_b, abs=1e-12)


def test_outside_the_chain_the_nearest_end_is_used_alone(ref_obj):
    chain = physics.Datum(ref_obj.elsets)
    for when, end in ((chain.epochs[0] - dt.timedelta(hours=6), 0),
                      (chain.epochs[-1] + dt.timedelta(days=3), -1)):
        r_obs = np.array([physics.propagate(chain.sats[end], when)[0]])
        assert chain.offsets([when], r_obs, 1.0)[0] == pytest.approx(0.0, abs=1e-9)


def test_two_element_sets_at_one_epoch_keep_the_first(ref_obj):
    first, second = ref_obj.elsets[0], ref_obj.elsets[1]
    twin = type(second)(epoch=first.epoch, line1=second.line1, line2=second.line2,
                        source="other originator")
    chain = physics.Datum([first, twin, *ref_obj.elsets[1:]])
    assert chain.elsets[0] is first
    assert len(chain.sats) == len(ref_obj.elsets)


def test_a_datum_needs_an_element_set():
    with pytest.raises(ComputeError):
        physics.Datum([])
    with pytest.raises(ComputeError, match="no TLEs"):
        physics.reference_chain([type("O", (), {"sat_no": REF, "elsets": []})()], REF)
    with pytest.raises(ComputeError, match="not among"):
        physics.reference_chain([], REF)


def test_the_longest_gap_is_reported_in_hours(ref_obj):
    chain = physics.Datum(ref_obj.elsets)
    gaps = np.diff([(e - chain.epochs[0]).total_seconds() for e in chain.epochs])
    assert chain.longest_gap_hours() == pytest.approx(gaps.max() / 3600.0)
    assert physics.Datum(ref_obj.elsets[:1]).longest_gap_hours() == 0.0
    assert physics.Datum(ref_obj.elsets[:1]).rolling is False


def test_speed_comes_from_the_element_set_in_force(ref_obj):
    chain = physics.Datum(ref_obj.elsets)
    when = chain.epochs[2] + dt.timedelta(hours=1)
    _r, v = physics.propagate(chain.sats[2], when)
    assert chain.speed_at(when) == pytest.approx(float(np.linalg.norm(v)))


# --------------------------------------------------------------------------- #
#  The report carries both readings
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def panel():
    from timeslides.demo import build_demo_modes
    from timeslides.report.builder import build_panel
    start = dt.datetime(2026, 6, 24)
    end = start + dt.timedelta(days=7)
    group = build_demo_modes(start, end)[0]
    return build_panel(0, group["name"], group["sat_order"], group["names"],
                       group["objects_by_mode"], ["REAL", "SIM"], group["reference"],
                       False, (start, end), None, first=True), group


def test_every_dataset_is_measured_against_both_datums(panel):
    p, _group = panel
    md = p["modeData"]["REAL"]
    rd = md["refData"][str(md["defaultRef"])]
    assert set(rd["alt"]) == {"x", "cards", "ahead"}
    assert len(rd["alt"]["x"]) == len(rd["x"])
    assert [len(a) for a in rd["alt"]["x"]] == [len(a) for a in rd["x"]]
    d = rd["datum"]
    assert d["count"] == len(d["epochs"]) > 1
    assert d["gapHours"] > 0


def test_the_alt_reading_is_the_old_datum(panel):
    from timeslides.report.builder import _present_sources
    p, group = panel
    md = p["modeData"]["REAL"]
    ref = md["defaultRef"]
    by_sat = group["objects_by_mode"]["REAL"]
    latest = physics.reference_elset(list(by_sat.values()), ref, None)
    present = _present_sources(group["objects_by_mode"], group["name"])
    want = []
    for s in group["sat_order"]:
        series = physics.compute_series(by_sat[s], latest.satrec(), False)
        want.extend([[off for _e, off, _src in series.get(k, [])] for k in present])
    assert md["refData"][str(ref)]["alt"]["x"] == want


def test_the_check_uses_only_the_reference_s_state_vectors(panel):
    p, group = panel
    md = p["modeData"]["REAL"]
    ref = md["defaultRef"]
    obj = group["objects_by_mode"]["REAL"][ref]
    check = md["refData"][str(ref)]["datum"]["check"]
    assert check["n"] == sum(len(v) for v in obj.state_series.values())
    assert check["rolling"] is not None and check["latest"] is not None


def test_a_reference_without_state_vectors_has_no_check():
    from timeslides.report.builder import _rms
    assert _rms({ELSET_KEY: [(None, 1.0, "")]}) == (0, None)
    assert _rms({"leolabs": [(None, 3.0, ""), (None, -4.0, "")]}) == (2, pytest.approx(3.536, abs=1e-3))


def test_the_chains_are_held_once_per_object_at_mode_level(panel):
    p, _group = panel
    md = p["modeData"]["REAL"]
    assert set(md["chains"]) == set(md["refData"])
    for norad, lines in md["chains"].items():
        assert len(lines) == md["refData"][norad]["datum"]["count"]
        assert all(len(pair) == 2 for pair in lines)
    assert "chain" not in md["refData"][str(md["defaultRef"])]


# --------------------------------------------------------------------------- #
#  A hand-entered point is measured against the datum the plot shows
# --------------------------------------------------------------------------- #
def test_a_manual_element_set_of_the_reference_sits_at_zero_on_the_chain(ref_obj):
    from timeslides.manual import plot_manual
    chain = [[e.line1, e.line2] for e in physics.Datum(ref_obj.elsets).elsets]
    pick = ref_obj.elsets[3]
    out = plot_manual({"kind": "elset", "elset": f"{pick.line1}\n{pick.line2}",
                       "datum": chain, "tle": chain[-1]})
    assert out["offset"] == pytest.approx(0.0, abs=1e-6)


def test_without_a_chain_the_manual_datum_is_the_one_element_set(ref_obj):
    from timeslides.manual import parse_datum
    pair = [ref_obj.elsets[-1].line1, ref_obj.elsets[-1].line2]
    datum = parse_datum({"tle": pair})
    assert len(datum.sats) == 1
    assert parse_datum({"tle": pair, "datum": []}).epochs == datum.epochs


def test_an_oversized_chain_is_refused(ref_obj):
    from timeslides.manual import MAX_CHAIN, parse_datum
    pair = [ref_obj.elsets[0].line1, ref_obj.elsets[0].line2]
    with pytest.raises(ValidationError, match="at most"):
        parse_datum({"datum": [pair] * (MAX_CHAIN + 1)})
    with pytest.raises(ValidationError, match="at most"):
        parse_datum({"datum": "not a list"})
