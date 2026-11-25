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
/* Recorded rather than discarded: a warning the code emits is part of what it
   does, and a test that asserts the fallback should be able to assert that the
   operator was told why. */
globalThis.__warnings = [];
globalThis.console = {
  log: function () {},
  warn: function () { globalThis.__warnings.push([].slice.call(arguments)); },
  error: function () {}
};
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


def test_a_refused_store_is_said_once_and_then_left_alone(picker):
    """Recorded rather than swallowed, and only once: repeating it on every
    redraw would bury the message in its own noise."""
    picker.eval("globalThis.__storageThrows = true;")
    _load_groups(picker, ["a", "b"])
    for _ in range(5):
        picker.eval("toggleGroup('a', false); toggleGroup('a', true);")
    warnings = _call(picker, "__warnings.map(w => w[0])")
    denials = [w for w in warnings if "will not let" in w]
    assert len(denials) == 1, warnings
    assert "resets on reload" in denials[0]
    assert "Everything else works" in denials[0]


def test_a_corrupt_stored_selection_falls_back_rather_than_throwing(picker):
    picker.eval('window.localStorage.setItem("timeslides.selectedGroups",'
                ' "not json");')
    assert _load_groups(picker, ["a", "b"]) == ["a", "b"]


@pytest.mark.parametrize("stored", ['"not json"', '"{\\"a\\":1}"', '"42"',
                                    '"null"'])
def test_a_stored_value_of_the_wrong_shape_is_discarded(picker, stored):
    picker.eval(f'window.localStorage.setItem("timeslides.selectedGroups", {stored});')
    assert _load_groups(picker, ["a", "b"]) == ["a", "b"]


def test_an_unreadable_stored_value_is_replaced_not_treated_as_a_denial(picker):
    """The distinction the two catches exist for. An unreadable value means the
    store works and its contents are simply not what this code wrote. Treating
    that as a refusal would stop every later write and leave the bad value in
    place for good, so the selection could never be remembered again."""
    picker.eval('window.localStorage.setItem("timeslides.selectedGroups",'
                ' "not json");')
    _load_groups(picker, ["a", "b"])
    picker.eval("toggleGroup('a', false);")
    stored = _call(
        picker, 'JSON.parse(window.localStorage.getItem("timeslides.selectedGroups"))')
    assert stored == ["b"], "the bad value must have been overwritten"
    warnings = _call(picker, "__warnings.map(w => w[0])")
    assert any("could not be read" in w for w in warnings), warnings
    assert not any("will not let" in w for w in warnings), \
        "a bad value is not a refusal to store"


# --------------------------------------------------------------------------- #
#  report.js: manual states
# --------------------------------------------------------------------------- #
def test_every_group_starts_with_no_manual_states(report_js):
    """Held in memory only, so a fresh page has none, whatever was plotted in
    the last one."""
    assert _call(report_js, "ST[0].manual") == []


def test_restyles_name_only_the_dataset_s_own_traces(report_js):
    """Plotly cycles a short update array across every trace on the plot. A
    restyle that did not name its traces would rewrite a manual state's trace
    with the first object's data, or hide it along with the first object."""
    got = _call(report_js, "ownTraces({traces: [{}, {}, {}]})")
    assert got == [0, 1, 2]
    assert _call(report_js, "ownTraces({traces: []})") == []


def test_a_refusal_carries_the_service_s_sentence(report_js):
    got = _call(report_js, 'refusal({detail: "looks like m/s"}).message')
    assert got == "looks like m/s"


def test_a_refusal_without_a_sentence_says_refused_rather_than_object(report_js):
    """A schema error's detail is a list, which would print as [object Object]."""
    assert _call(report_js, 'refusal({detail: [{"loc": ["x"]}]}).message') == "refused"
    assert _call(report_js, "refusal({}).message") == "refused"


# --------------------------------------------------------------------------- #
#  picker.js: requests go to this service's API and nowhere else
# --------------------------------------------------------------------------- #
FETCH_STUB = """
globalThis.__fetched = [];
globalThis.fetch = function (path) {
  globalThis.__fetched.push(path);
  return Promise.resolve({ ok: true, status: 200,
                           json: function () { return Promise.resolve({ fine: 1 }); } });
};
"""


def _settle(ctx, expression: str):
    """Run an async call to completion and return what it resolved or threw."""
    ctx.eval(f"globalThis.__out = null; ({expression})"
             ".then(v => { __out = {ok: v}; }, e => { __out = {err: e.message}; });")
    while ctx.execute_pending_job():
        pass
    return _call(ctx, "__out")


@pytest.mark.parametrize("path", [
    "https://elsewhere.example/api/groups",
    "//elsewhere.example/api/groups",
    "/healthz",
    "api/groups",
])
def test_a_request_outside_this_service_s_api_is_refused_before_it_is_sent(path):
    ctx = _context("picker.js", {"shell-data": SHELL_DATA})
    ctx.eval(FETCH_STUB)
    got = _settle(ctx, f"api({json.dumps(path)})")
    assert "refused to request" in got["err"]
    assert _call(ctx, "__fetched") == []


def test_a_request_to_this_service_s_api_is_sent():
    ctx = _context("picker.js", {"shell-data": SHELL_DATA})
    ctx.eval(FETCH_STUB)
    assert _settle(ctx, 'api("/api/groups")') == {"ok": {"fine": 1}}
    assert _call(ctx, "__fetched") == ["/api/groups"]


# --------------------------------------------------------------------------- #
#  report.js: the Look-ahead chooses a server-built view
# --------------------------------------------------------------------------- #
def test_without_a_look_ahead_in_the_payload_nothing_is_chosen():
    ctx = _context("report.js", {"report-data": REPORT_DATA})
    ctx.eval("GROUPS[0].modeData[ST[0].mode].refData[ST[0].ref] = {};")
    assert _call(ctx, "aheadHorizon(GROUPS[0])") is None
    assert _call(ctx, "aheadView(GROUPS[0])") is None


def test_the_server_s_default_horizon_applies_until_one_is_chosen():
    ctx = _context("report.js", {"report-data": REPORT_DATA})
    ctx.eval("GROUPS[0].modeData[ST[0].mode].refData[ST[0].ref] = {ahead:"
             " {default: '7', views: {'3': {items: 'three'}, '7': {items: 'seven'}}}};")
    assert _call(ctx, "aheadHorizon(GROUPS[0])") == "7"
    assert _call(ctx, "aheadView(GROUPS[0]).items") == "seven"
    ctx.eval("ST[0].horizon = '3';")
    assert _call(ctx, "aheadView(GROUPS[0]).items") == "three"


def test_a_card_says_what_its_drift_was_before_a_rate_change(report_js):
    assert _call(report_js, "driftNote({})") == ""
    got = _call(report_js, 'driftNote({driftNote: "since rate change · was +18.0"})')
    assert got == '<br><span class="driftnote">since rate change · was +18.0</span>'
    assert "&lt;b&gt;" in _call(report_js, 'driftNote({driftNote: "<b>"})')


# --------------------------------------------------------------------------- #
#  The datum (1.10)
# --------------------------------------------------------------------------- #
_DATUM_RD = ("{x: [[1, 2]], cards: ['rolling'], ahead: 'rolling-ahead', y: [['a', 'b']],"
             " alt: {x: [[9, 8]], cards: ['latest'], ahead: 'latest-ahead'},"
             " datum: {count: 3, epochs: ['e1', 'e2', 'e3'], gapHours: 30,"
             " check: {n: 12, rolling: 0.0412, latest: 0.873}}}")


@pytest.fixture
def datum_js():
    ctx = _context("report.js", {"report-data": REPORT_DATA})
    ctx.eval(f"GROUPS[0].modeData[ST[0].mode].refData[ST[0].ref] = {_DATUM_RD};")
    return ctx


def test_the_rolling_datum_is_the_default_reading(datum_js):
    assert _call(datum_js, "ST[0].datum") == "rolling"
    assert _call(datum_js, "dataset(GROUPS[0]).x") == [[1, 2]]
    assert _call(datum_js, "dataset(GROUPS[0]).cards") == ["rolling"]


def test_latest_only_overlays_offsets_cards_and_ahead_and_shares_the_rest(datum_js):
    datum_js.eval("ST[0].datum = 'latest';")
    assert _call(datum_js, "dataset(GROUPS[0]).x") == [[9, 8]]
    assert _call(datum_js, "dataset(GROUPS[0]).cards") == ["latest"]
    assert _call(datum_js, "dataset(GROUPS[0]).ahead") == "latest-ahead"
    assert _call(datum_js, "dataset(GROUPS[0]).y") == [["a", "b"]]


def test_the_datum_label_names_what_the_plot_is_measured_from(report_js):
    assert _call(report_js, "datumLabel({datum: 'rolling'}, {count: 7})") == "Rolling · 7 element sets"
    assert _call(report_js, "datumLabel({datum: 'rolling'}, {count: 1})") == (
        "Rolling · 1 element set, the same as latest")
    assert _call(report_js, "datumLabel({datum: 'latest'}, {count: 7})") == "Latest element set only"


def test_the_check_reports_both_datums_and_a_long_gap(report_js):
    got = _call(report_js, "datumCheck({gapHours: 30, check: {n: 12, rolling: 0.0412, latest: 0.873}})")
    assert "12 state vectors" in got
    assert "0.04 s RMS from zero against the rolling datum" in got
    assert "0.87 s against the latest element set alone" in got
    assert "Longest gap between reference element sets: 30 h" in got
    short = _call(report_js, "datumCheck({gapHours: 6, check: {n: 1, rolling: 0, latest: 0}})")
    assert "Longest gap" not in short
    assert "No independent check" in _call(report_js, "datumCheck({check: {n: 0}})")


def test_epoch_ticks_are_drawn_for_a_rolling_datum_only(datum_js):
    ticks = _call(datum_js, "datumShapes(GROUPS[0])")
    assert [t["y0"] for t in ticks] == ["e1", "e2", "e3"]
    assert all(t["name"] == "datum-epoch" for t in ticks)
    datum_js.eval("ST[0].datum = 'latest';")
    assert _call(datum_js, "datumShapes(GROUPS[0])") == []
