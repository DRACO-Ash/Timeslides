"""Unit tests for the shipped JavaScript, run under pytest.

Why this file exists
--------------------
The browser suite in test_smoke.py drives the assets for real, which is the
only way to prove the wiring works. But it needs a browser, so it skips
wherever one is not provisioned, including the platform's own test stage. That
left the JavaScript behaviourally tested on a developer machine and untested
everywhere else, and the quality gate, which measures rather than trusts, put
its line coverage at zero.

This closes that gap. A pip-installable JavaScript engine runs in process, so
the actual shipped files are loaded and their pure functions exercised on every
run, including in the pipeline. There is no copy of the logic here to drift out
of step: the tests load timeslides/report/assets/*.js as published.

What it does not do
-------------------
It does not produce a coverage report the SonarQube gate can read. Sonar wants
LCOV for JavaScript and this engine does not emit it, so the assets stay
excluded from the coverage metric with that rationale recorded in
sonar-project.properties. Tested, and honestly described as not line-measured.

The DOM wiring, event handlers and fetch calls stay the browser suite's job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

ASSETS = Path(__file__).resolve().parent.parent / "timeslides" / "report" / "assets"

# Enough of a browser for the top level of each file to run. Both assets read a
# JSON payload out of a script tag at load time and then register a load
# handler; neither touches anything else until an event fires.
STUB = """
globalThis.__handlers = {};
globalThis.__payloads = %s;
globalThis.document = {
  getElementById: function (id) {
    if (globalThis.__payloads[id] === undefined) return null;
    return { textContent: globalThis.__payloads[id] };
  },
  querySelectorAll: function () { return []; },
  body: { addEventListener: function () {} }
};
globalThis.window = {
  addEventListener: function (name, fn) { globalThis.__handlers[name] = fn; },
  /* The picker remembers which groups a render covers. A store that works is
     the normal case; __storageThrows exercises the one that does not, which is
     a private window, blocked site data, or a browser that throws on access. */
  localStorage: {
    _v: {},
    getItem: function (k) {
      if (globalThis.__storageThrows) throw new Error("denied");
      return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null;
    },
    setItem: function (k, v) {
      if (globalThis.__storageThrows) throw new Error("denied");
      this._v[k] = String(v);
    }
  }
};
"""

REPORT_DATA = {
    "groups": [{
        "id": 0, "divId": "waterfall_0", "traces": [],
        "present": ["leolabs", "elset"],
        "modeData": {"REAL": {"refData": {}, "refs": [], "defaultRef": 59884}},
        "modeOrder": ["REAL"], "defaultMode": "REAL", "defaultRef": 59884,
    }],
    "srclbl": {"leolabs": "LeoLabs", "northstar": "NorthStar", "kbr": "KBR",
               "ppec": "PPEC", "spacetrack": "Space-Track", "elset": "Element sets"},
}
SHELL_DATA = {"demo": True, "classification": "OFFICIAL",
              "storageWritable": True}


def _context(asset: str, payloads: dict):
    ctx = quickjs.Context()
    ctx.eval(STUB % json.dumps({k: json.dumps(v) for k, v in payloads.items()}))
    ctx.eval((ASSETS / asset).read_text(encoding="utf-8"))
    return ctx


def _call(ctx, expression: str):
    """Evaluate and bring the result back as Python, via JSON."""
    return json.loads(ctx.eval(f"JSON.stringify({expression})"))


@pytest.fixture(scope="module")
def report_js():
    return _context("report.js", {"report-data": REPORT_DATA})


@pytest.fixture(scope="module")
def picker_js():
    return _context("picker.js", {"shell-data": SHELL_DATA})


# --------------------------------------------------------------------------- #
#  Both files load at all
# --------------------------------------------------------------------------- #
def test_report_js_loads_and_registers_its_load_handler(report_js):
    assert _call(report_js, "typeof window.addEventListener") == "function"
    assert _call(report_js, "typeof __handlers.load") == "function"


def test_report_js_reads_its_payload_and_seeds_per_group_state(report_js):
    assert _call(report_js, "GROUPS.length") == 1
    assert _call(report_js, "ST[0].mode") == "REAL"
    assert _call(report_js, "ST[0].ref") == "59884"
    assert _call(report_js, "[...ST[0].sources].sort()") == ["elset", "leolabs"]
    assert _call(report_js, "GROUPS[0].vkms") == 1


def test_picker_js_loads_and_reads_its_payload(picker_js):
    assert _call(picker_js, "CFG.demo") is True
    assert _call(picker_js, "CFG.classification") == "OFFICIAL"
    assert _call(picker_js, "typeof __handlers.load") == "function"


def test_the_saved_group_list_carries_no_note_when_storage_persists(picker_js):
    assert _call(picker_js, "groupsStatus()") == ""


def test_the_saved_group_list_warns_when_groups_are_only_in_memory():
    """The banner at the top of the tab can be scrolled clear of the group
    list, so the list says it too."""
    ctx = _context("picker.js", {"shell-data": {**SHELL_DATA,
                                                "storageWritable": False}})
    note = _call(ctx, "groupsStatus()")
    assert "memory only" in note
    assert "lost when the pod restarts" in note


def test_picker_js_starts_with_an_empty_working_set(picker_js):
    assert _call(picker_js, "S.picked") == []
    assert _call(picker_js, "S.reference") is None
    assert _call(picker_js, "S.editing") is None


# --------------------------------------------------------------------------- #
#  Escaping, which is the security-relevant one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("asset", ["report.js", "picker.js"])
@pytest.mark.parametrize("raw,want", [
    ("<img src=x>", "&lt;img src=x&gt;"),
    ('"quoted"', "&quot;quoted&quot;"),
    ("it's", "it&#x27;s"),
    ("a & b", "a &amp; b"),
    ("</script>", "&lt;/script&gt;"),
    ("plain text", "plain text"),
    ("", ""),
])
def test_both_assets_escape_every_html_metacharacter(asset, raw, want):
    payload = {"report.js": {"report-data": REPORT_DATA},
               "picker.js": {"shell-data": SHELL_DATA}}[asset]
    ctx = _context(asset, payload)
    assert _call(ctx, f"esc({json.dumps(raw)})") == want


@pytest.mark.parametrize("asset", ["report.js", "picker.js"])
def test_escaping_a_missing_value_yields_an_empty_string(asset):
    """The templates interpolate optional fields; undefined must not print
    the word "undefined" into the page."""
    payload = {"report.js": {"report-data": REPORT_DATA},
               "picker.js": {"shell-data": SHELL_DATA}}[asset]
    ctx = _context(asset, payload)
    assert _call(ctx, "esc(undefined)") == ""
    assert _call(ctx, "esc(null)") == ""


# --------------------------------------------------------------------------- #
#  report.js: number formatting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,want", [
    (0, "+0.0"), (1.24, "+1.2"), (-1.26, "-1.3"), (-0.04, "-0.0"),
    (246.94, "+246.9"),
])
def test_offsets_are_formatted_with_an_explicit_sign(report_js, value, want):
    assert _call(report_js, f"fmt({value})") == want


def test_the_decimal_places_can_be_overridden(report_js):
    assert _call(report_js, "fmt(1.2345, 3)") == "+1.234"


@pytest.mark.parametrize("seconds,speed,want", [
    (-120, 7.5, 900.0), (120, 7.5, 900.0), (0, 7.5, 0.0),
])
def test_seconds_of_offset_convert_to_kilometres(report_js, seconds, speed, want):
    """Offset times orbital speed, sign discarded: it is a distance."""
    assert _call(report_js, f"km({seconds}, {speed})") == want


# --------------------------------------------------------------------------- #
#  report.js: the card fragments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("card,want", [
    ({"is_ref": False, "absent": False}, "card"),
    ({"is_ref": True, "absent": False}, "card ref"),
    ({"is_ref": False, "absent": True}, "card nodata"),
    ({"is_ref": True, "absent": True}, "card ref nodata"),
])
def test_card_classes_reflect_reference_and_missing_data(report_js, card, want):
    assert _call(report_js, f"cardClasses({json.dumps(card)})") == want


def test_the_reference_gets_a_chip_and_everything_else_an_eye(report_js):
    assert "refchip" in _call(report_js, 'cardBadge({"is_ref": true})')
    assert "REF" in _call(report_js, 'cardBadge({"is_ref": true})')
    assert "SHOWN" in _call(report_js, 'cardBadge({"is_ref": false})')


def test_the_counts_line_names_each_provider_with_its_total(report_js):
    card = {"counts": {"leolabs": 28, "northstar": 10, "kbr": 0}}
    got = _call(report_js, f"cardCounts({json.dumps(card)})")
    assert "LeoLabs 28" in got
    assert "NorthStar 10" in got
    assert "KBR" not in got, "a provider with no records should be omitted"


def test_the_counts_line_says_so_when_nothing_reported(report_js):
    assert _call(report_js, 'cardCounts({"counts": {}})') == "no data"
    assert _call(report_js, 'cardCounts({"counts": {"kbr": 0}})') == "no data"


def test_the_kilometre_line_covers_absent_reference_and_drifting(report_js):
    absent = _call(report_js, 'cardKmLine({"absent": true, "is_ref": false}, 7.5)')
    assert absent == "no data in this mode"

    reference = _call(report_js, 'cardKmLine({"absent": false, "is_ref": true}, 7.5)')
    assert "reference datum" in reference

    trailing = _call(
        report_js, 'cardKmLine({"absent": false, "is_ref": false, "current": -246.9}, 7.5)')
    assert trailing.startswith("trails reference by")
    assert "1852 km" in trailing

    leading = _call(
        report_js, 'cardKmLine({"absent": false, "is_ref": false, "current": 120}, 7.5)')
    assert leading.startswith("leads reference by")


def test_an_absent_object_is_reported_as_absent_before_anything_else(report_js):
    """Absent wins over is_ref: a reference with no data in this mode must not
    claim to be the datum for it."""
    got = _call(report_js, 'cardKmLine({"absent": true, "is_ref": true}, 7.5)')
    assert got == "no data in this mode"


# --------------------------------------------------------------------------- #
#  report.js: relative motion between a pair
# --------------------------------------------------------------------------- #
def test_a_pair_within_a_second_is_aligned(report_js):
    got = _call(report_js, "pairState(0.4, 0.0)")
    assert got["state"] == "ALIGNED"
    assert got["cls"] == "rel-al"
    assert got["eta"] == ""


def test_a_pair_with_a_gap_but_no_relative_drift_is_steady(report_js):
    got = _call(report_js, "pairState(120, 0.01)")
    assert got["state"] == "STEADY"
    assert got["cls"] == "rel-st"


def test_a_gap_closing_reports_an_estimated_time_to_align(report_js):
    """Opposite signs on gap and relative drift means the gap is shrinking."""
    got = _call(report_js, "pairState(-100, 10)")
    assert got["state"] == "CLOSING"
    assert got["cls"] == "rel-cl"
    assert "10 d to align" in got["eta"]


def test_a_gap_widening_is_separating_and_offers_no_estimate(report_js):
    got = _call(report_js, "pairState(-100, -10)")
    assert got["state"] == "SEPARATING"
    assert got["cls"] == "rel-sp"
    assert got["eta"] == ""


def test_alignment_is_checked_before_drift(report_js):
    """A tiny gap is aligned even when the drift rate is large."""
    assert _call(report_js, "pairState(0.5, 50)")["state"] == "ALIGNED"


@pytest.mark.parametrize("state,want", [
    ("CLOSING", "&rarr;&larr;"),
    ("SEPARATING", "&larr;&nbsp;&rarr;"),
    ("ALIGNED", "&mdash;"),
    ("STEADY", "&mdash;"),
])
def test_the_arrow_matches_the_state(report_js, state, want):
    assert _call(report_js, f'PAIR_LINK[{json.dumps(state)}] || "&mdash;"') == want


# --------------------------------------------------------------------------- #
#  picker.js: the small label helpers
# --------------------------------------------------------------------------- #
def test_a_provider_that_answered_says_so(picker_js):
    row = {"available": True, "udlSource": "LeoLabs"}
    assert _call(picker_js, f"chipTitle({json.dumps(row)})") == \
        "UDL source LeoLabs: answered"


def test_a_provider_with_no_data_and_no_error_says_no_data(picker_js):
    row = {"available": False, "udlSource": "PPEC", "error": None}
    assert _call(picker_js, f"chipTitle({json.dumps(row)})") == \
        "UDL source PPEC: no data"


def test_a_provider_that_errored_carries_the_reason(picker_js):
    row = {"available": False, "udlSource": "KBR",
           "error": "UDL rejected the credentials"}
    got = _call(picker_js, f"chipTitle({json.dumps(row)})")
    assert "no data" in got
    assert "rejected the credentials" in got


def test_progress_is_blank_until_a_total_is_known(picker_js):
    assert _call(picker_js, 'progressDetail({"progress": {}})') == ""
    assert _call(picker_js, 'progressDetail({"progress": {"total": 0}})') == ""
    assert _call(picker_js, "progressDetail({})") == ""


def test_progress_shows_the_fraction_and_the_current_group(picker_js):
    job = {"progress": {"done": 1, "total": 3, "current": "PRC SpacePlane 4"}}
    got = _call(picker_js, f"progressDetail({json.dumps(job)})")
    assert "1/3" in got
    assert "PRC SpacePlane 4" in got


def test_progress_omits_the_group_when_none_is_reported(picker_js):
    job = {"progress": {"done": 2, "total": 3, "current": ""}}
    assert _call(picker_js, f"progressDetail({json.dumps(job)})").strip() == "2/3"


# --------------------------------------------------------------------------- #
#  Choosing which groups a render covers
#
#  Rendering everything every time is the wrong default once there is more than
#  a couple of groups: slow, it fetches data nobody asked for, and it buries
#  the group somebody actually cares about behind tabs.
# --------------------------------------------------------------------------- #
@pytest.fixture
def picker():
    """A fresh context per test: the selection is stateful."""
    return _context("picker.js", {"shell-data": SHELL_DATA})


def _load_groups(ctx, ids):
    """Put a group list into state and run the reconciliation."""
    groups = json.dumps([{"id": i, "name": i, "sats": [1, 2], "reference": 1}
                         for i in ids])
    ctx.eval(f"S.groups = {groups}; reconcileSelection();")
    return _call(ctx, "[...S.selected].sort()")


@pytest.mark.parametrize("chosen,total,want", [
    (3, 3, "3 groups will be rendered"),
    (1, 1, "1 group will be rendered"),
    (2, 5, "2 of 5 groups will be rendered"),
    (1, 4, "1 of 4 groups will be rendered"),
    (0, 4, "No groups selected. Tick at least one to render."),
    (0, 0, ""),
])
def test_the_count_line_says_what_will_actually_be_rendered(picker, chosen,
                                                            total, want):
    assert _call(picker, f"groupCountLabel({chosen}, {total})") == want


def test_everything_is_selected_when_there_is_nothing_remembered(picker):
    """The behaviour the application had before there was a choice to make, so
    a first visit is not a blank page with a dead button."""
    assert _load_groups(picker, ["a", "b", "c"]) == ["a", "b", "c"]


def test_a_remembered_selection_is_restored(picker):
    picker.eval('window.localStorage.setItem("timeslides.selectedGroups",'
                ' JSON.stringify(["b"]));')
    assert _load_groups(picker, ["a", "b", "c"]) == ["b"]


def test_a_group_created_since_the_last_draw_arrives_selected(picker):
    """A group you have just built is one you want in the next render."""
    _load_groups(picker, ["a", "b"])
    picker.eval("S.selected.delete('a');")
    assert _load_groups(picker, ["a", "b", "new"]) == ["b", "new"]


def test_an_archived_group_drops_out_of_the_selection(picker):
    """Otherwise it sits there invisibly and gets rendered."""
    _load_groups(picker, ["a", "b", "c"])
    assert _load_groups(picker, ["a", "c"]) == ["a", "c"]


def test_the_selection_survives_a_reload(picker):
    """What "rather than every time" actually means: the choice sticks."""
    _load_groups(picker, ["a", "b", "c"])
    picker.eval("toggleGroup('b', false);")
    stored = _call(
        picker, 'JSON.parse(window.localStorage.getItem("timeslides.selectedGroups"))')
    assert sorted(stored) == ["a", "c"]


def test_toggling_a_group_off_and_on_again_is_symmetrical(picker):
    _load_groups(picker, ["a", "b"])
    picker.eval("toggleGroup('a', false);")
    assert _call(picker, "[...S.selected].sort()") == ["b"]
    picker.eval("toggleGroup('a', true);")
    assert _call(picker, "[...S.selected].sort()") == ["a", "b"]


def test_select_all_and_select_none(picker):
    _load_groups(picker, ["a", "b", "c"])
    picker.eval("selectEvery(false);")
    assert _call(picker, "[...S.selected]") == []
    picker.eval("selectEvery(true);")
    assert _call(picker, "[...S.selected].sort()") == ["a", "b", "c"]


def test_a_storage_that_refuses_does_not_break_the_selection(picker):
    """localStorage throws outright in a private window or with site data
    blocked. Remembering the choice is a convenience; being able to make one is
    not, so a refusal falls back to selecting everything and carries on."""
    picker.eval("globalThis.__storageThrows = true;")
    assert _load_groups(picker, ["a", "b"]) == ["a", "b"]
    picker.eval("toggleGroup('a', false);")
    assert _call(picker, "[...S.selected]") == ["b"]


def test_a_corrupt_stored_selection_falls_back_rather_than_throwing(picker):
    picker.eval('window.localStorage.setItem("timeslides.selectedGroups",'
                ' "not json");')
    assert _load_groups(picker, ["a", "b"]) == ["a", "b"]
