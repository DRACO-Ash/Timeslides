"""The Look-ahead: rate changes, forecasts and passes.

The fixture is the proximity-operations scenario from the change proposal,
built with the application's own demo generator, SGP4 and report builder: a
TARGET as reference, a CHASER 450 s behind closing at about 20 s/day that
raises its rate to about 90 s/day on day 4 with a phase-continuous burn, and an
INSPECTOR 200 s ahead falling back at 12 s/day. Before this change the
CHASER's card read +45 s/day and the relative-motion rows put its pass at about
twice the true time.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import numpy as np
import pytest

from timeslides import demo
from timeslides.demo import build_demo_modes
from timeslides.report import lookahead
from timeslides.report.builder import build_panel, render_report

START, END = dt.datetime(2026, 11, 9), dt.datetime(2026, 11, 16)
DEG_PER_S = 360.0 / (86400 / 15.2)


def _obj(sat_no, name, offset0, drift, rng):
    n = 15.2 * (1.0 + drift / 86400.0)
    return demo._make_demo_object(sat_no, name, START, END, n, 200.0 + offset0 * DEG_PER_S, rng)


def _burner(sat_no, name, offset0, before, after, burn_day, rng):
    """Two orbits joined at the burn, continuous in phase at that instant."""
    a = _obj(sat_no, name, offset0, before, rng)
    b = _obj(sat_no, name, offset0 + before * burn_day - after * burn_day, after, rng)
    t_b = START + dt.timedelta(days=burn_day)
    for key in a.state_series:
        a.state_series[key] = ([s for s in a.state_series[key] if s.epoch < t_b]
                               + [s for s in b.state_series[key] if s.epoch >= t_b])
    a.elsets = [e for e in a.elsets if e.epoch < t_b] + [e for e in b.elsets if e.epoch >= t_b]
    return a


def scenario_panel():
    """Also used by the browser suite, which opens the rendered report."""
    rng = np.random.default_rng(11)
    objs = [_obj(70001, "TARGET", 0.0, 0.0, rng),
            _burner(70002, "CHASER", -450.0, 20.0, 90.0, 4.0, rng),
            _obj(70003, "INSPECTOR", 200.0, -12.0, rng)]
    return build_panel(0, "Proximity operations", [70001, 70002, 70003],
                       {o.sat_no: o.name for o in objs}, {"REAL": {o.sat_no: o for o in objs}},
                       ["REAL"], 70001, False, (START, END), None, first=True)


def scenario_report() -> str:
    return render_report([scenario_panel()], "OFFICIAL", generated=END)


@pytest.fixture(scope="module")
def scenario():
    return scenario_panel()["modeData"]["REAL"]["refData"]["70001"]


def _cards(rd):
    return {c["name"]: c for c in rd["cards"]}


# --------------------------------------------------------------------------- #
#  The scenario the change was made for
# --------------------------------------------------------------------------- #
def test_the_chaser_s_card_shows_the_rate_it_flies_now_not_an_average(scenario):
    chaser = _cards(scenario)["CHASER"]
    assert chaser["drift"] == pytest.approx(87, abs=4)          # was +45, the average
    assert chaser["driftNote"].startswith("since rate change · was +1")


def test_a_track_without_a_change_keeps_its_card_untouched(scenario):
    assert "driftNote" not in _cards(scenario)["INSPECTOR"]
    assert "driftNote" not in _cards(scenario)["TARGET"]


def test_the_seven_day_view_names_the_change_then_the_passes_in_order(scenario):
    items = scenario["ahead"]["views"]["7"]["items"]
    order = [m.group(0) for m in re.finditer(r"RATE CHANGE|in \d+\.\d d", items)]
    assert order[0] == "RATE CHANGE"
    assert order[1:] == ["in 1.2 d", "in 2.2 d", "in 9.8 d"]
    assert "<b>CHASER</b> passes <b>TARGET</b>" in items
    assert "<b>CHASER</b> passes <b>INSPECTOR</b>" in items
    assert 'class="ev ev-far"' in items, "the pass beyond the horizon is named, faded"
    assert "probable manoeuvre · forecast uses only the data after it</span>" in items, \
        "the note must finish its sentence: an earlier version ended on 'since'"


def test_the_plot_says_rate_change_and_never_asserts_intent(scenario):
    """A screenshot in a briefing should state what the data shows."""
    view = scenario["ahead"]["views"]["7"]
    texts = " ".join(a["text"] for a in view["annotations"])
    assert "rate change" in texts
    assert "manoeuvre" not in texts.replace("assumes no further manoeuvre", "")


def test_each_horizon_draws_only_what_falls_inside_it(scenario):
    views = scenario["ahead"]["views"]
    passes = {h: next(t for t in views[h]["traces"] if t["uid"] == "ahead-passes")["x"]
              for h in ("3", "7", "14")}
    assert len(passes["3"]) == 2 and len(passes["7"]) == 2 and len(passes["14"]) == 3
    line = next(t for t in views["14"]["traces"] if t["uid"] == "ahead-line-70002")
    assert line["y"][-1] == "2026-11-30T00:00:00"
    assert views["0"]["traces"] == [] and "Off." in views["0"]["items"]
    assert scenario["ahead"]["default"] == "7"


def test_the_band_widens_with_time_and_never_collapses_to_the_scatter(scenario):
    line = next(t for t in scenario["ahead"]["views"]["14"]["traces"]
                if t["uid"] == "ahead-line-70002")
    half = line["customdata"]
    assert half == sorted(half)
    assert half[-1] >= lookahead.RATE_ALLOWANCE * 14 - 0.01


# --------------------------------------------------------------------------- #
#  Units
# --------------------------------------------------------------------------- #
def _noisy(t, y, seed=3, sd=0.2):
    return y + np.random.default_rng(seed).normal(0, sd, len(t))


def test_fit_line_recovers_a_line():
    t = np.linspace(-7, 0, 50)
    a, b, s, *_ = lookahead.fit_line(t, 5.0 + 3.0 * t)
    assert (a, b) == (pytest.approx(5.0), pytest.approx(3.0))
    assert s == pytest.approx(0.0, abs=1e-9)


def test_a_rate_change_is_found_where_it_happened():
    t = np.linspace(-7, 0, 120)
    y = _noisy(t, np.where(t < -3, 10 * (t + 3), 60 * (t + 3)))
    change = lookahead.find_rate_change(t, y)
    assert change.t == pytest.approx(-3, abs=0.15)
    assert (change.before, change.after) == (pytest.approx(10, abs=1), pytest.approx(60, abs=1))


def test_a_straight_track_has_no_change():
    t = np.linspace(-7, 0, 120)
    assert lookahead.find_rate_change(t, _noisy(t, 4 * t)) is None


def test_a_significant_but_tiny_change_is_not_reported():
    """The prototype flagged a 0.1 s/day 'change' on clean data. Strong
    evidence is not enough; the change has to matter."""
    t = np.linspace(-7, 0, 200)
    y = _noisy(t, np.where(t < -3, 10 * (t + 3), 11 * (t + 3)), sd=0.001)
    assert lookahead.find_rate_change(t, y) is None


def test_too_few_points_to_judge_a_change_is_no_change():
    t = np.linspace(-1, 0, 2 * lookahead.MIN_SIDE)
    assert lookahead.find_rate_change(t, 10 * t) is None


def test_a_perfect_two_segment_track_does_not_break_the_arithmetic():
    """No noise at all means a residual of zero, which has no logarithm."""
    t = np.linspace(-7, 0, 60)
    change = lookahead.find_rate_change(t, np.where(t < -3, 0.0, 30 * (t + 3)))
    assert change is not None and change.after == pytest.approx(30, abs=1)


def _track(name, a, b, s=0.0, ref=False):
    return lookahead.Track(1, name, "#4C9BE8", ref, a=a, b=b, s=s, tbar=-3.5, sxx=200.0, m=50)


def test_converging_tracks_pass_where_their_lines_meet():
    found = lookahead.passes([_track("A", -100, 50), _track("B", 0, 0, ref=True)], 30)
    assert len(found) == 1
    assert found[0].t == pytest.approx(2.0)
    assert found[0].x == pytest.approx(0.0)
    assert (found[0].mover, found[0].other) == ("A", "B")


@pytest.mark.parametrize(("a", "b", "why"), [
    (-100, -50, "diverging"),
    (-100, 0.01, "parallel within the steady threshold"),
    (-0.5, 50, "already aligned"),
    (-100, 2, "meets after the limit"),
])
def test_no_pass_is_claimed_when_there_is_none(a, b, why):
    assert lookahead.passes([_track("A", a, b), _track("B", 0, 0, ref=True)], 30) == [], why


def test_a_pass_already_behind_us_is_not_a_forecast():
    assert lookahead.passes([_track("A", 100, 50), _track("B", 0, 0, ref=True)], 30) == []


def test_with_no_scatter_the_band_is_the_model_allowance():
    assert lookahead.half_width(_track("A", 0, 1), 5.0) == pytest.approx(
        lookahead.RATE_ALLOWANCE * 5.0, rel=1e-6)
    assert lookahead.half_width(_track("R", 0, 0, ref=True), 5.0) == 0.0


@pytest.mark.parametrize(("half_days", "want"), [
    (20 / 1440, "± 20 min"), (5 / 24, "± 5.0 h"), (3.0, "± 3.0 d")])
def test_the_band_is_written_in_a_sensible_unit(half_days, want):
    assert lookahead._band_text(half_days) == want


def test_passes_are_capped_at_the_nearest_few():
    """Sixteen objects passing the reference every half day: eleven or more
    inside seven days, two beyond. Eight are listed, then the next beyond."""
    tracks = [_track("REF", 0, 0, ref=True)] + [
        _track(f"O{i}", -10.0 * (i + 1), 20.0) for i in range(16)]
    items = lookahead.view(tracks, END, 7)["items"]
    assert items.count("passes") == lookahead.MAX_PASSES + 1
    assert items.count("ev-far") == 1


def test_a_quiet_week_says_so():
    view = lookahead.view([_track("REF", 0, 0, ref=True), _track("A", -100, -5)], END, 7)
    assert "Nothing passes and no rate changes" in view["items"]


def test_a_track_that_cannot_be_forecast_is_left_out():
    now = END
    few = [(now - dt.timedelta(days=1), 1.0), (now, 2.0)]
    instant = [(now, 1.0), (now, 2.0), (now, 3.0)]
    assert lookahead.track_for(1, "A", "#fff", False, few, now) is None
    assert lookahead.track_for(1, "A", "#fff", False, instant, now) is None


def test_names_are_escaped_for_each_sink_they_reach():
    """Plotly text and the rail's HTML want different escaping. A name with an
    apostrophe must read normally on the plot, and markup must be inert in both."""
    hostile = "O'Brien <img src=x onerror=alert(1)>"
    tracks = [_track("REF", 0, 0, ref=True), _track(hostile, -100, 50)]
    tracks[1].change = lookahead.Change(t=-3, x=-250, before=10, after=50)
    view = lookahead.view(tracks, END, 7)
    plot_text = json.dumps(view["annotations"]) + json.dumps(view["traces"])
    assert "<img" not in plot_text and "O'Brien" in plot_text
    assert "<img" not in view["items"] and "O&#x27;Brien" in view["items"]


# --------------------------------------------------------------------------- #
#  On the application's own demo data
# --------------------------------------------------------------------------- #
def test_the_demo_raises_no_false_rate_change_anywhere():
    """Every group, both modes, every reference. A forecast that cries wolf on
    clean data is a forecast nobody reads."""
    for pid, grp in enumerate(build_demo_modes(START, END)):
        panel = build_panel(pid, grp["name"], grp["sat_order"], grp["names"],
                            grp["objects_by_mode"], ["REAL", "SIM"], grp["reference"],
                            False, (START, END), None, first=pid == 0)
        for md in panel["modeData"].values():
            for rd in md["refData"].values():
                assert all("driftNote" not in c for c in rd["cards"])
                assert "RATE CHANGE" not in rd["ahead"]["views"]["7"]["items"]


def test_the_report_carries_the_look_ahead_and_its_controls():
    grp = build_demo_modes(START, END)[0]
    panel = build_panel(0, grp["name"], grp["sat_order"], grp["names"], grp["objects_by_mode"],
                        ["REAL"], grp["reference"], False, (START, END), None, first=True)
    html = render_report([panel], "OFFICIAL", generated=END)
    assert 'id="ahead-0"' in html
    for h in ("0", "3", "7", "14"):
        assert f'data-h="{h}"' in html
    assert '<button type="button" data-h="7" class="active">7 d</button>' in html
