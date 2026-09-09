"""The fetch-and-build pipeline: validation, caching, and partial failure."""

from __future__ import annotations

import datetime as dt

import pytest

from timeslides.demo import build_demo_modes
from timeslides.errors import ComputeError, ValidationError
from timeslides.models import STATE_SOURCE_KEYS
from timeslides.pipeline import (MAX_GROUPS_PER_RUN, MAX_WINDOW_DAYS, Fetcher, RunSpec,
                                 build_report, demo_report, validate_modes,
                                 validate_sources, validate_window)

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
        key = {"LeoLabs": "leolabs", "NorthStar": "northstar", "KBR": "kbr"}[source]
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
    with pytest.raises(ValidationError, match=f"maximum is {MAX_WINDOW_DAYS}"):
        validate_window(START, START + dt.timedelta(days=MAX_WINDOW_DAYS + 1))
    validate_window(START, START + dt.timedelta(days=MAX_WINDOW_DAYS))


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
    assert _spec().key() == _spec().key()
    assert _spec().key() != _spec(invert=True).key()
    assert _spec().key() != _spec(modes=("SIM",)).key()
    assert _spec().key() != _spec(classification="SECRET").key()


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
    with pytest.raises(ComputeError, match="no group produced any usable data"):
        build_report([_group()], udl, _spec())


def test_no_groups_is_a_validation_error():
    with pytest.raises(ValidationError, match="no groups selected"):
        build_report([], FakeUDL(), _spec())


def test_too_many_groups_in_one_run_is_refused():
    groups = [_group(f"G{i}", (59884, 67689), 59884)
              for i in range(MAX_GROUPS_PER_RUN + 1)]
    with pytest.raises(ValidationError, match="maximum per run"):
        build_report(groups, FakeUDL(), _spec())


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
