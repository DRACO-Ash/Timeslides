"""The fetch-and-build pipeline: validation, caching, and partial failure."""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from timeslides.demo import build_demo_modes
from timeslides.errors import ComputeError, ValidationError
from timeslides.models import ELSET_KEY, STATE_SOURCES, STATE_SOURCE_KEYS
from timeslides.pipeline import (MAX_GROUPS_PER_RUN, MAX_WINDOW_DAYS, Fetcher, RunSpec,
                                 build_report, demo_report, validate_modes,
                                 validate_sources, validate_window)

# Derived from the source table rather than hardcoded, so adding a provider
# does not silently leave the fake client behind.
_KEY_BY_UDL_SOURCE = {s["udl_source"]: s["key"] for s in STATE_SOURCES}

START = dt.datetime(2026, 6, 24)
END = dt.datetime(2026, 7, 1)


class FakeUDL:
    """Serves the demo objects' data through the client interface, so the
    pipeline can be exercised with no network."""

    def __init__(self, groups=None, empty_for=(), raise_for=()):
        groups = groups or build_demo_modes(START, END)
        self.objects = {}
        self.names = {}
        for grp in groups:
            for mode, by_sat in grp["objects_by_mode"].items():
                for sat_no, obj in by_sat.items():
                    self.objects[(sat_no, mode)] = obj
                    self.names[sat_no] = grp["names"].get(sat_no, f"OBJECT {sat_no}")
        self.empty_for = set(empty_for)
        self.raise_for = set(raise_for)
        self.sv_calls = []
        self.elset_calls = []
        self.catalogue_calls = 0

    def objects_by_satno(self, sat_nos):
        self.catalogue_calls += 1
        return {n: dict(satNo=n, name=self.names.get(n, f"OBJECT {n}"))
                for n in sat_nos if n in self.names}

    def state_vectors(self, sat_no, start, end, source="LeoLabs",
                      data_mode="REAL", default_frame="J2000"):
        self.sv_calls.append((sat_no, source, data_mode))
        if sat_no in self.raise_for:
            raise ComputeError("boom")
        if sat_no in self.empty_for:
            return []
        mode = "SIM" if data_mode == "SIMULATED" else "REAL"
        obj = self.objects.get((sat_no, mode))
        # Derived from the source table rather than hardcoded, so adding a
        # provider does not silently leave this fake behind.
        key = _KEY_BY_UDL_SOURCE[source]
        return list(obj.state_series.get(key, [])) if obj else []

    def elsets(self, sat_no, start, end, data_mode="REAL"):
        self.elset_calls.append((sat_no, data_mode))
        if sat_no in self.empty_for:
            return []
        mode = "SIM" if data_mode == "SIMULATED" else "REAL"
        obj = self.objects.get((sat_no, mode))
        return list(obj.elsets) if obj else []


def _spec(**over):
    base = dict(start=START, end=END, modes=("REAL",), sources=tuple(STATE_SOURCE_KEYS))
    base.update(over)
    return RunSpec(**base)


def _group(name="PRC Spaceplane", sats=(59884, 67689, 69673), reference=59884):
    return dict(id=name, name=name, sats=list(sats), reference=reference)


# --------------------------------------------------------------------------- #
#  Window validation
# --------------------------------------------------------------------------- #
def test_a_normal_window_passes():
    assert validate_window(START, END) == (START, END)


def test_a_window_must_run_forwards():
    with pytest.raises(ValidationError, match="after its start"):
        validate_window(END, START)
    with pytest.raises(ValidationError, match="after its start"):
        validate_window(START, START)


@pytest.mark.parametrize("start,end", [(None, END), (START, None), (None, None)])
def test_both_ends_of_the_window_are_required(start, end):
    with pytest.raises(ValidationError, match="both a start and an end"):
        validate_window(start, end)


def test_an_absurdly_long_window_is_refused():
    """The fan-out is objects x providers x modes requests each capped at
    maxResults. A year for a dozen objects exhausts the UDL budget for
    everyone else sharing the pod."""
    over = START + dt.timedelta(days=MAX_WINDOW_DAYS + 1)
    with pytest.raises(ValidationError, match=f"maximum is {MAX_WINDOW_DAYS}"):
        validate_window(START, over)
    # The boundary itself is allowed.
    at_limit = START + dt.timedelta(days=MAX_WINDOW_DAYS)
    assert validate_window(START, at_limit) == (START, at_limit)


# --------------------------------------------------------------------------- #
#  Mode and source validation
# --------------------------------------------------------------------------- #
def test_modes_default_to_real_and_deduplicate():
    assert validate_modes(None) == ("REAL",)
    assert validate_modes([]) == ("REAL",)
    assert validate_modes(["SIM", "REAL", "SIM"]) == ("SIM", "REAL")


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValidationError, match="unknown data mode"):
        validate_modes(["REAL", "PRETEND"])


def test_sources_default_to_all_and_deduplicate():
    assert validate_sources(None) == tuple(STATE_SOURCE_KEYS)
    assert validate_sources(["kbr", "kbr"]) == ("kbr",)


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValidationError, match="unknown state provider"):
        validate_sources(["leolabs", "acme"])


# --------------------------------------------------------------------------- #
#  RunSpec identity, which is the single-flight key
# --------------------------------------------------------------------------- #
def test_identical_specs_share_a_key_and_different_ones_do_not():
    # Two separately built specs, not the same expression twice: the point is
    # that equal inputs produce an equal key, which needs two objects.
    first = _spec()
    second = _spec()
    assert first is not second
    assert first.key() == second.key()
    assert first.key() != _spec(invert=True).key()
    assert first.key() != _spec(modes=("SIM",)).key()
    assert first.key() != _spec(classification="SECRET").key()


def test_a_spec_is_hashable_so_it_can_index_the_single_flight_table():
    assert len({_spec(), _spec(), _spec(invert=True)}) == 2


# --------------------------------------------------------------------------- #
#  Building
# --------------------------------------------------------------------------- #
def test_a_report_is_built_from_udl_data():
    udl = FakeUDL()
    html = build_report([_group()], udl, _spec())
    assert html.startswith("<!DOCTYPE html>")
    assert "PRC Spaceplane" in html
    assert udl.catalogue_calls == 1


def test_object_names_come_from_the_udl_catalogue():
    udl = FakeUDL()
    html = build_report([_group()], udl, _spec())
    assert "OBJECT G" in html


def test_each_object_is_fetched_once_even_across_groups():
    """Two groups sharing an object must not pay for it twice."""
    udl = FakeUDL()
    groups = [_group("A", (59884, 67689), 59884), _group("B", (59884, 69673), 59884)]
    build_report(groups, udl, _spec())
    assert [c for c in udl.elset_calls].count((59884, "REAL")) == 1


def test_only_the_requested_providers_are_called():
    udl = FakeUDL()
    build_report([_group()], udl, _spec(sources=("leolabs",)))
    assert {c[1] for c in udl.sv_calls} == {"LeoLabs"}


def test_both_data_modes_are_fetched_when_asked_for():
    udl = FakeUDL()
    build_report([_group()], udl, _spec(modes=("REAL", "SIM")))
    assert {c[2] for c in udl.sv_calls} == {"REAL", "SIMULATED"}


def test_a_group_with_no_data_is_skipped_not_fatal():
    """One dead group should not cost you the others."""
    udl = FakeUDL(empty_for=(40001, 40002, 40003))
    groups = [_group(), _group("LEO Cluster", (40001, 40002, 40003), 40001)]
    html = build_report(groups, udl, _spec())
    assert "PRC Spaceplane" in html
    assert "LEO Cluster" not in html


def test_if_nothing_can_be_built_that_is_an_error():
    udl = FakeUDL(empty_for=(59884, 67689, 69673))
    groups, spec = [_group()], _spec()
    with pytest.raises(ComputeError, match="no group produced any usable data"):
        build_report(groups, udl, spec)


def test_no_groups_is_a_validation_error():
    udl, spec = FakeUDL(), _spec()
    with pytest.raises(ValidationError, match="no groups selected"):
        build_report([], udl, spec)


def test_too_many_groups_in_one_run_is_refused():
    groups = [_group(f"G{i}", (59884, 67689), 59884)
              for i in range(MAX_GROUPS_PER_RUN + 1)]
    udl, spec = FakeUDL(), _spec()
    with pytest.raises(ValidationError, match="maximum per run"):
        build_report(groups, udl, spec)


def test_progress_is_reported_once_per_group():
    seen = []
    udl = FakeUDL()
    groups = [_group("A", (59884, 67689), 59884), _group("B", (59884, 69673), 59884)]
    build_report(groups, udl, _spec(),
                 progress=lambda current, done, total: seen.append((current, done, total)))
    assert [s[0] for s in seen] == ["A", "B"]
    assert all(s[2] == 2 for s in seen)


def test_a_reference_outside_the_group_falls_back_to_the_first_member():
    """Defence in depth: the store already rejects this, but a stored document
    edited by hand should not crash a render."""
    udl = FakeUDL()
    html = build_report([_group(reference=99999)], udl, _spec())
    assert html.startswith("<!DOCTYPE html>")


# --------------------------------------------------------------------------- #
#  The Fetcher's cache
# --------------------------------------------------------------------------- #
def test_the_fetcher_caches_per_satellite_and_mode():
    udl = FakeUDL()
    f = Fetcher(udl, _spec(modes=("REAL", "SIM")), {})
    first = f.get(59884, "REAL")
    assert f.get(59884, "REAL") is first
    assert f.get(59884, "SIM") is not first
    assert len(udl.elset_calls) == 2


def test_the_fetcher_counts_its_calls_for_the_audit_line():
    udl = FakeUDL()
    f = Fetcher(udl, _spec(sources=("leolabs", "northstar")), {})
    f.get(59884, "REAL")
    assert f.calls == 3          # two providers plus one element-set call


def test_all_five_providers_are_fetched_by_default():
    """LeoLabs, NorthStar, KBR, PPEC and Space-Track, all as state vectors."""
    udl = FakeUDL()
    build_report([_group()], udl, _spec())
    assert {c[1] for c in udl.sv_calls} == {
        "LeoLabs", "NorthStar", "KBR", "PPEC", "Space-Track"}


def test_the_default_source_list_is_every_state_provider():
    assert validate_sources(None) == ("leolabs", "northstar", "kbr", "ppec",
                                      "spacetrack")


def test_the_element_set_series_is_not_a_selectable_provider():
    """It is always plotted and is where the reference orbit comes from, so it
    is not something a caller can switch off by naming providers."""
    with pytest.raises(ValidationError, match="unknown state provider"):
        validate_sources([ELSET_KEY])


# --------------------------------------------------------------------------- #
#  Demo mode
# --------------------------------------------------------------------------- #
def test_demo_mode_renders_with_no_client_at_all():
    html = demo_report(_spec())
    assert html.startswith("<!DOCTYPE html>")
    assert "PRC Spaceplane" in html
    assert "LEO Cluster" in html


def test_demo_mode_supplies_its_own_window_when_none_is_given():
    html = demo_report(RunSpec())
    assert "24 Jun 2026" in html


def test_demo_mode_honours_the_requested_providers():
    """Without this the Configure tab's provider toggles looked broken in demo
    mode: the synthetic generator produces every provider regardless."""
    html = demo_report(_spec(sources=("leolabs",)))
    # Checked on the legend chips, not by substring: SRC_LABEL is embedded
    # wholesale as the client's label lookup, so every provider's name appears
    # in the payload whether or not it is plotted.
    assert 'data-src="leolabs"' in html
    for absent in ("northstar", "kbr", "ppec", "spacetrack"):
        assert f'data-src="{absent}"' not in html, absent


def test_demo_mode_always_keeps_the_element_set_series():
    """It anchors the reference orbit, so it is not optional."""
    html = demo_report(_spec(sources=("leolabs",)))
    assert f'data-src="{ELSET_KEY}"' in html


# --------------------------------------------------------------------------- #
#  Ingestion catches duplication before it reaches a chart
#
#  Duplication is invisible on the plot by nature: two reports at one epoch
#  overplot, so the picture looks identical whether a source sent one or five.
#  That is the whole reason it has to be caught on the way in.
# --------------------------------------------------------------------------- #
class _RepeatingClient(FakeUDL):
    """A feed that repeats itself: every state vector twice, and one element
    set duplicated plus one disagreeing at an epoch that already has one."""

    def state_vectors(self, sat_no, start, end, source="LeoLabs",
                      data_mode="REAL", default_frame="J2000"):
        svs = super().state_vectors(sat_no, start, end, source, data_mode,
                                    default_frame)
        return [*svs, *(copy.deepcopy(sv) for sv in svs)]

    def elsets(self, sat_no, start, end, data_mode="REAL"):
        els = super().elsets(sat_no, start, end, data_mode)
        if not els:
            return els
        clash = copy.deepcopy(els[0])
        clash.line2 = clash.line2[:20] + "9" + clash.line2[21:]
        return [*els, copy.deepcopy(els[0]), clash]


def _fetch_one(client, sat_no=59884):
    from timeslides.pipeline import Fetcher

    fetcher = Fetcher(client, _spec(), client.names)
    return fetcher.get(sat_no, "REAL")


def test_a_repeated_state_vector_is_collapsed_before_the_series_is_built():
    groups = build_demo_modes(START, END)
    obj = _fetch_one(_RepeatingClient(groups))
    clean = _fetch_one(FakeUDL(build_demo_modes(START, END)))
    for key, series in clean.state_series.items():
        assert len(obj.state_series[key]) == len(series), key


def test_the_repeated_records_are_reported_as_findings_on_the_object():
    obj = _fetch_one(_RepeatingClient(build_demo_modes(START, END)))
    assert obj.findings, "duplication has to be reported, not silently dropped"
    duplicates = sum(f["duplicates"] for f in obj.findings)
    assert duplicates > 0
    assert all(f["received"] > f["plotted"] for f in obj.findings)


def test_a_disagreeing_element_set_is_reported_as_a_conflict():
    """Distinct from a duplicate, because which record is plotted changes what
    the chart says."""
    obj = _fetch_one(_RepeatingClient(build_demo_modes(START, END)))
    conflicts = [c for f in obj.findings for c in f["conflicts"]]
    assert len(conflicts) == 1, conflicts


def test_a_clean_feed_produces_no_findings_at_all():
    """The common case stays quiet, or the band becomes furniture."""
    obj = _fetch_one(FakeUDL(build_demo_modes(START, END)))
    assert obj.findings == []


def test_two_originators_at_one_epoch_are_not_treated_as_duplication():
    """The element-set query is not filtered by source. Two producers reporting
    the same object at the same epoch is two independent element sets, and
    collapsing them would delete a report the operator is entitled to see."""
    groups = build_demo_modes(START, END)

    class TwoSources(FakeUDL):
        def elsets(self, sat_no, start, end, data_mode="REAL"):
            els = super().elsets(sat_no, start, end, data_mode)
            if not els:
                return els
            other = copy.deepcopy(els[0])
            other.source = "Some Other Provider"
            other.line2 = other.line2[:20] + "9" + other.line2[21:]
            return [*els, other]

    obj = _fetch_one(TwoSources(groups))
    baseline = _fetch_one(FakeUDL(build_demo_modes(START, END)))
    assert len(obj.elsets) == len(baseline.elsets) + 1
    assert obj.findings == []


def test_the_findings_reach_the_report_panel():
    """On the object is not enough; the point is that somebody sees them."""
    from timeslides.report.builder import _quality_band

    groups = build_demo_modes(START, END)
    client = _RepeatingClient(groups)
    html = build_report([_group()], client, _spec())
    assert "Data quality" in html
    assert "Same-epoch disagreement" in html
    assert _quality_band({"findings": [], "duplicates": 0}) == ""
