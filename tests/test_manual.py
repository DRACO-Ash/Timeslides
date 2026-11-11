"""Hand-entered state vectors, placed on a report's waterfall and never stored.

The property that matters most is the first one tested: a manual state is
measured by exactly the path a UDL state vector is. If the two disagreed, the
feature would show a hand-entered state somewhere other than where the same
state from the UDL would land, which is the one thing it exists to answer.
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from timeslides import manual, physics
from timeslides.api import create_app
from timeslides.config import Settings
from timeslides.demo import build_demo
from timeslides.errors import ValidationError
from timeslides.groups import GroupStore
from timeslides.jobs import JobRunner
from timeslides.models import StateVector

START = dt.datetime(2026, 6, 24)
END = dt.datetime(2026, 7, 1)
REF = 59884


@pytest.fixture(scope="module")
def demo_objects():
    return build_demo(START, END)


@pytest.fixture(scope="module")
def ref(demo_objects):
    """The reference element set exactly as the report carries it."""
    els = physics.reference_elset(demo_objects, REF, None)
    return manual.parse_reference([els.line1, els.line2])


@pytest.fixture(scope="module")
def observed(demo_objects):
    """A real state vector from an object that is not the reference."""
    obj = next(o for o in demo_objects if o.sat_no != REF and o.state_series)
    key, svs = next(iter(obj.state_series.items()))
    return key, svs


def _body(sv: StateVector, ref, **extra) -> dict:
    body = {"epoch": sv.epoch.isoformat() + "Z", "frame": sv.frame,
            "x": str(sv.r[0]), "y": str(sv.r[1]), "z": str(sv.r[2]),
            "vx": str(sv.v[0]), "vy": str(sv.v[1]), "vz": str(sv.v[2]),
            "tle": [ref.line1, ref.line2], "invert": False}
    body.update(extra)
    return body


# --------------------------------------------------------------------------- #
#  It lands where the same state from the UDL would
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("frame", ["TEME", "J2000"])
def test_a_manual_state_lands_exactly_where_the_series_path_puts_it(ref, observed, frame):
    """Both frames, against the vectorised path the report itself uses. The
    scalar and batched projections differ only in summation order, documented
    in physics as up to about 1e-13 s, so the tolerance is far below anything
    visible and far above that."""
    _key, svs = observed
    for sv in svs[:5]:
        as_frame = StateVector(epoch=sv.epoch, r=sv.r, v=sv.v, frame=frame)
        series = physics._state_offsets([as_frame], ref.satrec(), 1.0)[0][1]
        offset, _speed = manual.manual_offset(as_frame, ref, invert=False)
        assert offset == pytest.approx(series, abs=1e-9)


def test_the_end_to_end_result_matches_the_series_path(ref, observed):
    """Through plot_manual, so the string parsing is in the loop too."""
    _key, svs = observed
    sv = svs[len(svs) // 2]
    out = manual.plot_manual(_body(sv, ref))
    series = physics._state_offsets([sv], ref.satrec(), 1.0)[0][1]
    assert out["offset"] == pytest.approx(series, abs=1e-3)
    assert out["trace"]["x"] == [pytest.approx(series, abs=1e-9)]
    assert out["trace"]["y"] == [sv.epoch.isoformat()]


def test_the_report_carries_the_reference_it_actually_anchored_on(demo_objects, ref):
    """The datum sent back is the latest element set, the same choice the
    series are measured from, not a second selection that could differ."""
    obj = next(o for o in demo_objects if o.sat_no == REF)
    latest = max(obj.elsets, key=lambda e: e.epoch)
    assert (ref.line1, ref.line2) == (latest.line1, latest.line2)
    assert abs((ref.epoch - latest.epoch).total_seconds()) < 1e-3


def test_invert_flips_the_sign(ref, observed):
    _key, svs = observed
    plain, _ = manual.manual_offset(svs[0], ref, invert=False)
    flipped, _ = manual.manual_offset(svs[0], ref, invert=True)
    assert flipped == pytest.approx(-plain)
    assert plain != 0


def test_kilometres_use_the_reference_speed_at_the_state_epoch(ref, observed):
    _key, svs = observed
    out = manual.plot_manual(_body(svs[0], ref))
    _r, v = physics.propagate(ref.satrec(), svs[0].epoch)
    assert out["km"] == pytest.approx(abs(out["offset"]) * np.linalg.norm(v), abs=0.2)


# --------------------------------------------------------------------------- #
#  It cannot be mistaken for UDL data
# --------------------------------------------------------------------------- #
def test_the_trace_is_marked_on_the_chart_itself(ref, observed):
    """The on-chart text is what survives a PNG export; the tooltip is what a
    hover shows. Both have to say it."""
    _key, svs = observed
    trace = manual.plot_manual(_body(svs[0], ref, label="Plan B burn"))["trace"]
    assert trace["uid"].startswith("manual-")
    assert trace["marker"]["symbol"] == "star"
    assert trace["text"] == ["MANUAL · Plan B burn"]
    assert "not from UDL" in trace["hovertemplate"]
    assert "not saved" in trace["hovertemplate"]
    assert "Plan B burn" in trace["hovertemplate"]
    assert trace["showlegend"] is False


def test_an_unlabelled_state_is_still_marked(ref, observed):
    _key, svs = observed
    out = manual.plot_manual(_body(svs[0], ref))
    assert out["trace"]["text"] == ["MANUAL"]
    assert "<br><br>" not in out["trace"]["hovertemplate"]
    assert "Manual state" in out["row"]


def test_the_rail_row_says_temporary_and_can_be_removed(ref, observed):
    _key, svs = observed
    out = manual.plot_manual(_body(svs[0], ref, label="check"))
    uid = out["trace"]["uid"]
    assert f'data-uid="{uid}"' in out["row"]
    assert "TEMPORARY" in out["row"]
    assert 'class="manx"' in out["row"]
    assert out["detail"] in out["row"]
    assert ">STATE<" in out["row"]


def test_the_manual_colour_is_no_object_s_colour():
    from timeslides.report.builder import COOL_PALETTE, REF_COLOUR
    assert manual.MANUAL_COLOUR not in [*COOL_PALETTE, REF_COLOUR]


def test_each_state_gets_its_own_identity(ref, observed):
    _key, svs = observed
    a = manual.plot_manual(_body(svs[0], ref))["trace"]["uid"]
    b = manual.plot_manual(_body(svs[0], ref))["trace"]["uid"]
    assert a != b


# --------------------------------------------------------------------------- #
#  Parsing at the boundary, with refusals that say what to fix
# --------------------------------------------------------------------------- #
def test_an_epoch_keeps_its_fraction_of_a_second():
    """A second of epoch error is a second of offset at LEO speed."""
    assert manual.parse_epoch("2026-06-25T12:00:00.250Z") == dt.datetime(
        2026, 6, 25, 12, 0, 0, 250000)


def test_an_epoch_with_an_offset_is_converted_to_utc():
    assert manual.parse_epoch("2026-06-25T13:00:00+01:00") == dt.datetime(2026, 6, 25, 12)


def test_a_naive_epoch_is_taken_as_utc():
    assert manual.parse_epoch("2026-06-25T12:00:00") == dt.datetime(2026, 6, 25, 12)


@pytest.mark.parametrize(("raw", "match"), [
    ("", "epoch is required"),
    ("yesterday", "not an ISO 8601 time"),
])
def test_a_bad_epoch_is_refused(raw, match):
    with pytest.raises(ValidationError, match=match):
        manual.parse_epoch(raw)


def test_the_frame_defaults_to_j2000_and_is_case_insensitive():
    assert manual.parse_frame(None) == "J2000"
    assert manual.parse_frame("teme") == "TEME"


def test_an_unsupported_frame_is_refused():
    with pytest.raises(ValidationError, match="frame must be one of J2000, TEME"):
        manual.parse_frame("ITRF")


@pytest.mark.parametrize(("raw", "match"), [
    ("abc", "x must be a number in km"),
    ("inf", "x must be a finite number in km"),
    ("", "x must be a number in km"),
])
def test_a_component_that_is_not_a_finite_number_is_refused(ref, observed, raw, match):
    _key, svs = observed
    with pytest.raises(ValidationError, match=match):
        manual.plot_manual(_body(svs[0], ref, x=raw))


def test_numbers_are_accepted_as_numbers_as_well_as_text(ref, observed):
    _key, svs = observed
    body = _body(svs[0], ref)
    body["x"] = float(body["x"])
    assert manual.plot_manual(body)["offset"] == manual.plot_manual(_body(svs[0], ref))["offset"]


@pytest.mark.parametrize(("r", "v", "match"), [
    ((6_778_137.0, 0.0, 0.0), (0.0, 7.6, 0.0), "looks like metres"),
    ((6000.0, 0.0, 0.0), (0.0, 7.6, 0.0), "inside the Earth"),
    ((6778.0, 0.0, 0.0), (0.0, 7600.0, 0.0), "looks like m/s"),
    ((6778.0, 0.0, 0.0), (0.0, 0.0, 0.0), "effectively zero"),
    ((6778.0, 0.0, 0.0), (0.0, 0.0, 1e-6), "effectively zero"),
])
def test_an_impossible_state_is_refused_with_the_likely_unit_error(r, v, match):
    with pytest.raises(ValidationError, match=match):
        manual.check_state(np.array(r), np.array(v))


def test_a_plain_label_is_kept():
    assert manual.parse_label("  Plan B (rev 2) #3  ") == "Plan B (rev 2) #3"
    assert manual.parse_label(None) == ""


def test_a_label_that_is_too_long_is_refused():
    with pytest.raises(ValidationError, match="limited to 40"):
        manual.parse_label("x" * 41)


@pytest.mark.parametrize("raw", ["<script>", "a&b", 'say "hi"', "it's"])
def test_a_label_with_markup_characters_is_refused(raw):
    """Refused rather than escaped, so there is no question of which escaper
    the chart's tooltip and the rail each want."""
    with pytest.raises(ValidationError, match="letters, digits, spaces"):
        manual.parse_label(raw)


@pytest.mark.parametrize("raw", [None, "1 abc", ["only one line"], ["a", "b", "c"]])
def test_a_reference_that_is_not_two_lines_is_refused(raw):
    with pytest.raises(ValidationError, match="must be two TLE lines"):
        manual.parse_reference(raw)


def test_a_reference_of_the_wrong_shape_is_refused(ref):
    with pytest.raises(ValidationError, match="not a two-line element set"):
        manual.parse_reference([ref.line1[:-1], ref.line2])
    with pytest.raises(ValidationError, match="not a two-line element set"):
        manual.parse_reference([ref.line2, ref.line1])


def test_a_reference_the_parser_cannot_read_is_refused():
    """The parser returns rather than raises on a malformed set, so this is
    the error code being checked, not an exception being caught."""
    with pytest.raises(ValidationError, match="could not be parsed"):
        manual.parse_reference(["1 " + "x" * 67, "2 " + "y" * 67])


def test_a_state_too_far_from_the_reference_epoch_is_refused(ref, observed):
    _key, svs = observed
    far = (ref.epoch + dt.timedelta(days=120)).isoformat() + "Z"
    with pytest.raises(ValidationError, match="SGP4 is not meaningful beyond 90 days"):
        manual.plot_manual(_body(svs[0], ref, epoch=far))


# --------------------------------------------------------------------------- #
#  The endpoint
# --------------------------------------------------------------------------- #
def _app(tmp_path):
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    runner = JobRunner(lambda spec, progress: "<html/>", settings.runs_path, workers=1)
    return create_app(settings=settings, store=GroupStore(settings.groups_file),
                      runner=runner), runner


@pytest.fixture
def client(tmp_path):
    app, runner = _app(tmp_path)
    with TestClient(app) as c:
        yield c
    runner.shutdown()


def test_the_endpoint_places_a_state_and_records_only_that_it_did(
        capsys, tmp_path, ref, observed):
    """The audit line says a manual state was plotted and in which frame. It
    does not carry the vector, the label or the element set: none of that is
    the service's to keep.

    The app is built inside the test, as in test_api, so its log handler binds
    to the stdout that capsys is capturing.
    """
    _key, svs = observed
    app, runner = _app(tmp_path)
    with TestClient(app) as c:
        res = c.post("/api/manual-state", json=_body(svs[0], ref, label="secret plan"))
    runner.shutdown()
    assert res.status_code == 200
    assert res.json()["trace"]["uid"].startswith("manual-")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()
             if line.startswith("{")]
    plotted = [line["attributes"] for line in lines if line["body"] == "manual_state.plotted"]
    assert len(plotted) == 1
    assert plotted[0]["labelled"] is True
    assert plotted[0]["kind"] == "state"
    record = json.dumps(plotted[0])
    for leaked in ("secret plan", ref.line1, f"{svs[0].r[0]}"):
        assert leaked not in record


def test_the_endpoint_refuses_with_a_sentence_not_a_schema(client, ref, observed):
    _key, svs = observed
    res = client.post("/api/manual-state", json=_body(svs[0], ref, vx="7600"))
    assert res.status_code == 400
    assert isinstance(res.json()["detail"], str)
    assert "looks like m/s" in res.json()["detail"]


def test_the_endpoint_writes_nothing_to_the_volume(client, ref, observed, tmp_path):
    _key, svs = observed
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert client.post("/api/manual-state", json=_body(svs[0], ref)).status_code == 200
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_the_on_chart_label_points_towards_zero(ref, observed):
    """An outlying state is where the axis ends, so a label pointing away
    from zero is clipped by the plot edge. Seen in a screenshot, not a test."""
    _key, svs = observed
    ahead = manual._trace("manual-a", "state", svs[0].epoch, 140.0, "x", "TEME")
    behind = manual._trace("manual-b", "elset", svs[0].epoch, -140.0, "x", "TEME")
    assert ahead["textposition"] == "middle left"
    assert behind["textposition"] == "middle right"



# --------------------------------------------------------------------------- #
#  Element sets
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def other_elsets(demo_objects):
    """Real element sets from an object that is not the reference."""
    obj = next(o for o in demo_objects if o.sat_no != REF and o.elsets)
    return sorted(obj.elsets, key=lambda e: e.epoch)


def _elset_body(els, ref, **extra) -> dict:
    body = {"kind": "elset", "elset": f"{els.line1}\n{els.line2}",
            "tle": [ref.line1, ref.line2], "invert": False}
    body.update(extra)
    return body


def test_a_pasted_element_set_lands_where_the_element_set_series_puts_it(ref, other_elsets):
    """Against physics._tle_offsets, the path the report uses for every element
    set. The pasted set's epoch comes from the set itself, whose text carries
    it to about a millisecond, so the tolerance allows for that rounding and
    nothing visible."""
    for els in other_elsets[:5]:
        series = physics._tle_offsets([els], ref.satrec(), 1.0)[0][1]
        pasted = manual.parse_pasted_elset(f"{els.line1}\n{els.line2}")
        offset, _speed = manual.elset_offset(pasted, ref, invert=False)
        assert offset == pytest.approx(series, abs=1e-6)


def test_the_reference_s_own_element_set_lands_on_zero(ref):
    out = manual.plot_manual(_elset_body(ref, ref))
    assert out["offset"] == pytest.approx(0.0, abs=1e-6)


def test_a_pasted_element_set_is_drawn_open_and_named_as_an_element_set(ref, other_elsets):
    """Filled means measured and open means derived, the plot's convention, so
    the pasted set is the open form of the manual star."""
    out = manual.plot_manual(_elset_body(other_elsets[0], ref, label="TLE from ops"))
    trace = out["trace"]
    assert trace["marker"]["symbol"] == "star-open"
    assert trace["text"] == ["MANUAL ELSET · TLE from ops"]
    assert "MANUAL ELEMENT SET" in trace["hovertemplate"]
    assert "not from UDL" in trace["hovertemplate"]
    norad = other_elsets[0].line1[2:7].strip()
    assert out["detail"] == f"NORAD {norad} · TEME"
    assert ">ELSET<" in out["row"] and "TEMPORARY" in out["row"]
    assert out["kind"] == "elset"


def test_an_unlabelled_element_set_is_named_as_one(ref, other_elsets):
    out = manual.plot_manual(_elset_body(other_elsets[0], ref))
    assert "Manual element set" in out["row"]
    assert out["trace"]["text"] == ["MANUAL ELSET"]


def test_a_name_line_windows_endings_and_trailing_spaces_are_tolerated(other_elsets):
    els = other_elsets[0]
    pasted = manual.parse_pasted_elset(f"OBJECT X  \r\n{els.line1}  \r\n{els.line2}\r\n\r\n")
    assert (pasted.line1, pasted.line2) == (els.line1, els.line2)


@pytest.mark.parametrize("raw", ["", "1 only one line", "a\nb\nc\nd"])
def test_anything_but_two_lines_is_refused(raw):
    with pytest.raises(ValidationError, match="paste the two lines"):
        manual.parse_pasted_elset(raw)


def test_a_pasted_set_of_the_wrong_shape_is_refused(other_elsets):
    els = other_elsets[0]
    with pytest.raises(ValidationError, match="the element set is not a two-line element set"):
        manual.parse_pasted_elset(f"{els.line1[:60]}\n{els.line2}")


def test_a_pasted_set_the_parser_cannot_read_is_refused():
    with pytest.raises(ValidationError, match="the element set could not be parsed"):
        manual.parse_pasted_elset("1 " + "x" * 67 + "\n2 " + "y" * 67)


def test_a_copying_error_is_caught_by_the_checksum(other_elsets):
    """One wrong digit moves the point with no other sign, which is what the
    checksum exists to catch."""
    els = other_elsets[0]
    i = els.line2.index(".") + 1             # a digit inside the inclination
    wrong = "1" if els.line2[i] != "1" else "2"
    typo = els.line2[:i] + wrong + els.line2[i + 1:]
    with pytest.raises(ValidationError, match=r"line 2 checksum is .* copying error"):
        manual.parse_pasted_elset(f"{els.line1}\n{typo}")


def test_lines_from_two_different_objects_are_refused(ref, other_elsets):
    """Each line's own checksum is valid, so only the object numbers say the
    two do not belong together."""
    with pytest.raises(ValidationError, match="both lines must be from the same element set"):
        manual.parse_pasted_elset(f"{other_elsets[0].line1}\n{ref.line2}")


def test_the_checksum_counts_digits_and_minus_signs_only():
    assert manual.tle_checksum("1 2-3" + " " * 64) == 7
    assert manual.tle_checksum("abc+." + " " * 64) == 0


def test_an_element_set_too_far_from_the_reference_is_refused(ref, other_elsets, monkeypatch):
    far = manual.Elset(epoch=ref.epoch + dt.timedelta(days=120),
                       line1=other_elsets[0].line1, line2=other_elsets[0].line2)
    monkeypatch.setattr(manual, "parse_pasted_elset", lambda raw: far)
    with pytest.raises(ValidationError, match="SGP4 is not meaningful beyond 90 days"):
        manual.plot_manual(_elset_body(other_elsets[0], ref))


def test_an_unknown_kind_is_refused_and_the_default_is_a_state():
    assert manual.parse_kind(None) == "state"
    assert manual.parse_kind(" ELSET ") == "elset"
    with pytest.raises(ValidationError, match="kind must be one of state, elset"):
        manual.parse_kind("ephemeris")


def test_the_endpoint_places_a_pasted_element_set(client, ref, other_elsets):
    res = client.post("/api/manual-state", json=_elset_body(other_elsets[0], ref))
    assert res.status_code == 200
    assert res.json()["kind"] == "elset"
    bad = client.post("/api/manual-state",
                      json=_elset_body(other_elsets[0], ref, elset="not a set"))
    assert bad.status_code == 400
    assert "paste the two lines" in bad.json()["detail"]
