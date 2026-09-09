"""Report rendering, and the escaping that the move to a service made necessary."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from timeslides.demo import build_demo_modes
from timeslides.errors import ComputeError
from timeslides.models import Elset, ObjectData
from timeslides.report import builder
from timeslides.report.builder import build_panel, esc, json_for_html, render_report

START = dt.datetime(2026, 6, 24)
END = dt.datetime(2026, 7, 1)
STAMP = dt.datetime(2026, 9, 9, 12, 0, 0)


def _panels(groups=None, name_override=None):
    groups = groups or build_demo_modes(START, END)
    out = []
    for pid, grp in enumerate(groups):
        out.append(build_panel(
            pid, name_override or grp["name"], grp["sat_order"], grp["names"],
            grp["objects_by_mode"], ["REAL", "SIM"], grp["reference"],
            False, (START, END), None, first=(pid == 0)))
    return out


@pytest.fixture(scope="module")
def panels():
    return _panels()


@pytest.fixture(scope="module")
def html(panels):
    return render_report(panels, "UNCLASSIFIED", generated=STAMP)


def _payload(doc):
    block = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', doc, re.S)
    return json.loads(block.group(1))


# --------------------------------------------------------------------------- #
#  Structure
# --------------------------------------------------------------------------- #
def test_report_is_a_self_contained_document(html):
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    # Plotly is inlined once, not fetched from a CDN.
    assert "plotly" in html
    assert "<style>" in html


def test_every_panel_gets_a_tab_and_a_section(html, panels):
    for p in panels:
        assert f'data-tab="{p["id"]}"' in html
        assert f'data-panel="{p["id"]}"' in html
        assert f'id="refsel-{p["id"]}"' in html
        assert f'id="cards-{p["id"]}"' in html


def test_classification_banner_is_rendered_upper_case(panels):
    doc = render_report(panels, "official sensitive", generated=STAMP)
    assert "OFFICIAL SENSITIVE" in doc


def test_payload_carries_one_entry_per_panel(html, panels):
    data = _payload(html)
    assert len(data["groups"]) == len(panels)
    assert data["srclbl"]["spacetrack"] == "Space-Track"
    for entry, p in zip(data["groups"], panels, strict=True):
        assert entry["divId"] == p["div_id"]
        assert set(entry["modeData"]) == {"REAL", "SIM"}


def test_mode_selector_appears_only_with_more_than_one_mode(panels):
    doc = render_report(panels, "UNCLASSIFIED", generated=STAMP)
    assert "seg modeseg" in doc
    single = _panels()
    for p in single:
        p["modeOrder"] = ["REAL"]
    assert "seg modeseg" not in render_report(single, "UNCLASSIFIED", generated=STAMP)


def test_rendering_no_panels_is_an_error_not_an_empty_page():
    with pytest.raises(ComputeError, match="no panels"):
        render_report([], "UNCLASSIFIED")


# --------------------------------------------------------------------------- #
#  Escaping. Group names arrive over HTTP; object names come from a UDL tenant.
# --------------------------------------------------------------------------- #
def test_esc_neutralises_every_html_metacharacter():
    assert esc('<a href="x">&\'') == "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;"


def test_json_for_html_cannot_close_the_script_block():
    out = json_for_html({"n": "</script><script>alert(1)</script>"})
    assert "</script>" not in out
    assert "\\u003c" in out
    # Still valid JSON carrying the original text.
    assert json.loads(out)["n"] == "</script><script>alert(1)</script>"


def test_a_hostile_group_name_cannot_break_out_of_the_markup():
    """The payload text may legitimately contain the characters of an attack
    string; what must never happen is those characters forming live markup.
    So the property under test is that no <img element exists and no script
    block is closed early, not that the substring is absent."""
    hostile = '</script><img src=x onerror=alert(1)><script>"\''
    doc = render_report(_panels(name_override=hostile), "UNCLASSIFIED", generated=STAMP)
    assert "<img" not in doc.lower()
    assert "</script><img" not in doc
    # The escaped form is what reaches the markup.
    assert "&lt;img src=x onerror=alert(1)&gt;" in doc
    # The JSON block is still parseable, so the early-close did not land.
    assert _payload(doc) is not None
    # And the name survives intact as data for the client to escape again.
    assert _panels(name_override=hostile)[0]["name"] == hostile


def test_a_hostile_object_name_is_escaped_in_the_figure_hovertemplate():
    groups = build_demo_modes(START, END)
    victim = groups[0]["sat_order"][0]
    groups[0]["names"][victim] = '<img src=x onerror=alert(1)>'
    doc = render_report(_panels(groups), "UNCLASSIFIED", generated=STAMP)
    # Plotly renders hovertemplate as HTML, so the name must arrive escaped.
    assert "<img" not in doc.lower()
    assert "&lt;img src=x onerror=alert(1)&gt;" in doc


def test_client_side_sinks_all_apply_esc():
    """A static check on the asset. Every interpolation of a name field into an
    innerHTML template must go through esc(). If someone adds a raw ${x.name}
    this fails, which is the point: the gate cannot see into a template string."""
    js = (builder.ASSETS / "report.js").read_text(encoding="utf-8")
    assert "function esc(v)" in js
    raw = re.findall(r"\$\{\s*[a-z]+\.name\s*\}", js)
    assert raw == [], f"unescaped name interpolations in report.js: {raw}"


# --------------------------------------------------------------------------- #
#  Statistics and colours
# --------------------------------------------------------------------------- #
def test_series_stats_of_an_empty_series_is_none():
    assert builder._series_stats([]) is None


def test_series_stats_reports_last_value_count_and_drift():
    pts = [(START + dt.timedelta(days=i), float(i) * 2.0) for i in range(4)]
    got = builder._series_stats(pts)
    assert got["n"] == 4
    assert got["current"] == 6.0
    assert got["drift"] == pytest.approx(2.0)


def test_series_stats_of_a_single_point_reports_zero_drift():
    got = builder._series_stats([(START, 5.0)])
    assert got == dict(current=5.0, drift=0.0, n=1)


def test_reference_object_gets_the_copper_colour_and_others_do_not():
    colours = builder._sat_colours([10, 20, 30], 20)
    assert colours[20] == builder.REF_COLOUR
    assert colours[10] != builder.REF_COLOUR
    assert colours[30] != builder.REF_COLOUR
    assert colours[10] != colours[30]


def test_colours_wrap_when_there_are_more_objects_than_palette_entries():
    sats = list(range(len(builder.COOL_PALETTE) + 3))
    colours = builder._sat_colours(sats, ref_no=None)
    assert set(colours.values()) <= set(builder.COOL_PALETTE)


# --------------------------------------------------------------------------- #
#  Panel assembly failure modes
# --------------------------------------------------------------------------- #
def test_a_group_with_no_data_at_all_raises_compute_error():
    empty = {"REAL": {1: ObjectData(sat_no=1, name="EMPTY", colour="#fff")}}
    with pytest.raises(ComputeError, match="no state or TLE data"):
        build_panel(0, "Empty", [1], {1: "EMPTY"}, empty, ["REAL"], 1,
                    False, (START, END), None, first=True)


def test_objects_without_tles_cannot_anchor_but_do_not_break_the_panel():
    """An object with state vectors but no element sets still plots; it just
    cannot be chosen as the waterfall reference."""
    groups = build_demo_modes(START, END)
    grp = groups[0]
    orphan = grp["sat_order"][-1]
    for by_sat in grp["objects_by_mode"].values():
        by_sat[orphan].elsets = []
    panel = build_panel(0, grp["name"], grp["sat_order"], grp["names"],
                        grp["objects_by_mode"], ["REAL"], grp["reference"],
                        False, (START, END), None, first=True)
    anchors = {r["norad"] for r in panel["modeData"]["REAL"]["refs"]}
    assert orphan not in anchors
    assert len(anchors) == len(grp["sat_order"]) - 1


def test_default_reference_falls_back_when_the_requested_one_cannot_anchor():
    groups = build_demo_modes(START, END)
    grp = groups[0]
    wanted = grp["reference"]
    for by_sat in grp["objects_by_mode"].values():
        by_sat[wanted].elsets = []
    panel = build_panel(0, grp["name"], grp["sat_order"], grp["names"],
                        grp["objects_by_mode"], ["REAL"], wanted,
                        False, (START, END), None, first=True)
    assert panel["defaultRef"] != wanted


def test_trace_layout_is_fixed_so_client_side_restyle_stays_aligned(panels):
    """The client toggles visibility by trace index, so every object must
    contribute exactly one trace per present source, in order."""
    for p in panels:
        n_objects = len(p["modeData"][p["defaultMode"]]["refs"])
        assert len(p["traces"]) % len(p["present"]) == 0
        assert n_objects >= 1
        for i, t in enumerate(p["traces"]):
            assert t["source"] == p["present"][i % len(p["present"])]


def test_write_report_creates_parent_directories(tmp_path, html):
    out = builder.write_report(html, tmp_path / "nested" / "deep" / "r.html")
    assert out.exists()
    assert out.read_text(encoding="utf-8") == html


# --------------------------------------------------------------------------- #
#  Reference anchoring: the paths where a candidate reference is unusable
# --------------------------------------------------------------------------- #
# A high drag term with a mean motion near re-entry: SGP4 returns error 1 a few
# days past epoch, so propagating this object fails rather than merely being
# absent. That is the difference between "cannot anchor" and "has no data".
DECAYING_L1 = "1 25544U 98067A   26175.50000000  .00016717  00000-0  99999-1 0  9005"
DECAYING_L2 = "2 25544  51.6400 208.9163 0006317  69.9862 290.1789 16.49309620 10005"


def _with_unpropagatable_reference():
    """A group where one member can be plotted but cannot anchor the waterfall.

    Its element set is stamped at exactly the epoch inside the two-line set
    (day 175.5 of 2026, so 24 June 12:00), which means propagating it to its
    own epoch works and it plots as a member. Anchoring on it means propagating
    it across the whole window, six days past epoch, where SGP4 returns error
    1. Stamping it later instead makes even its own point fail, which takes
    every candidate reference down with it rather than just this one.
    """
    groups = build_demo_modes(START, END)
    grp = groups[0]
    victim = grp["sat_order"][1]
    at_epoch = dt.datetime(2026, 6, 24, 12, 0)
    for by_sat in grp["objects_by_mode"].values():
        by_sat[victim].elsets = [Elset(epoch=at_epoch, line1=DECAYING_L1,
                                       line2=DECAYING_L2)]
    return grp, victim


def test_an_object_whose_orbit_cannot_be_propagated_is_dropped_as_an_anchor():
    """It has element sets, so it is a candidate, but building its dataset
    raises. The panel keeps the references that do work."""
    grp, victim = _with_unpropagatable_reference()
    panel = build_panel(0, grp["name"], grp["sat_order"], grp["names"],
                        grp["objects_by_mode"], ["REAL"], grp["reference"],
                        False, (START, END), None, first=True)
    anchors = {r["norad"] for r in panel["modeData"]["REAL"]["refs"]}
    assert victim not in anchors
    assert len(anchors) >= 1


def test_a_mode_with_nothing_to_anchor_on_is_skipped_not_fatal():
    """SIM has state vectors but no element sets, so no reference can be built
    for it. REAL still renders and the mode selector drops SIM."""
    groups = build_demo_modes(START, END)
    grp = groups[0]
    for obj in grp["objects_by_mode"]["SIM"].values():
        obj.elsets = []
    panel = build_panel(0, grp["name"], grp["sat_order"], grp["names"],
                        grp["objects_by_mode"], ["REAL", "SIM"], grp["reference"],
                        False, (START, END), None, first=True)
    assert panel["modeOrder"] == ["REAL"]
    assert "SIM" not in panel["modeData"]


def test_no_mode_having_an_anchor_is_an_error():
    """State vectors alone cannot produce a waterfall: the reference orbit
    comes from the element sets."""
    groups = build_demo_modes(START, END)
    grp = groups[0]
    for by_sat in grp["objects_by_mode"].values():
        for obj in by_sat.values():
            obj.elsets = []
    with pytest.raises(ComputeError, match="no usable data in any requested mode"):
        build_panel(0, grp["name"], grp["sat_order"], grp["names"],
                    grp["objects_by_mode"], ["REAL", "SIM"], grp["reference"],
                    False, (START, END), None, first=True)
