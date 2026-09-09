"""Fetch from the UDL and build panels. The body of the old main() function.

main() was 156 lines with 47 branch tokens, well past the App Store cognitive
complexity cap of 15, and it interleaved argument parsing, credential prompting,
fetching, rendering, file writing and opening a browser. The fetching and
panel-building parts live here, take their inputs as a RunSpec, and know nothing
about HTTP or the command line.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .audit import event
from .errors import ComputeError, ValidationError
from .models import (DATA_MODES, ObjectData, STATE_SOURCES, STATE_SOURCE_KEYS)
from .report.builder import build_panel, render_report

MAX_WINDOW_DAYS = 90
MAX_GROUPS_PER_RUN = 12


@dataclass(frozen=True)
class RunSpec:
    """Everything that determines a report. Frozen and hashable, so it doubles
    as the single-flight key: two identical requests are one render."""

    group_ids: tuple = ()
    start: dt.datetime = None
    end: dt.datetime = None
    modes: tuple = ("REAL",)
    sources: tuple = tuple(STATE_SOURCE_KEYS)
    invert: bool = False
    ref_epoch: dt.datetime = None
    classification: str = "UNCLASSIFIED"

    def key(self) -> tuple:
        return (self.group_ids, self.start, self.end, self.modes, self.sources,
                self.invert, self.ref_epoch, self.classification)


def validate_window(start, end) -> tuple:
    """A window must run forwards and stay within a sane length.

    The upper bound is not arbitrary: the fan-out is objects x providers x
    modes requests, each capped at maxResults records, and a year-long window
    for a dozen objects is a request that will not finish inside anybody's
    patience and will exhaust the UDL budget for everyone else using the pod.
    """
    if start is None or end is None:
        raise ValidationError("both a start and an end are required")
    if end <= start:
        raise ValidationError("the window end must be after its start")
    days = (end - start).total_seconds() / 86400.0
    if days > MAX_WINDOW_DAYS:
        raise ValidationError(
            f"the window is {days:.0f} days; the maximum is {MAX_WINDOW_DAYS}")
    return start, end


def validate_modes(raw) -> tuple:
    modes = tuple(dict.fromkeys(raw or ("REAL",)))
    unknown = [m for m in modes if m not in DATA_MODES]
    if unknown:
        raise ValidationError(
            f"unknown data mode(s) {unknown}; expected any of {sorted(DATA_MODES)}")
    return modes


def validate_sources(raw) -> tuple:
    sources = tuple(dict.fromkeys(raw or STATE_SOURCE_KEYS))
    unknown = [s for s in sources if s not in STATE_SOURCE_KEYS]
    if unknown:
        raise ValidationError(
            f"unknown state provider(s) {unknown}; expected any of {STATE_SOURCE_KEYS}")
    return sources


class Fetcher:
    """Pulls one object's data per (satellite, mode) and caches it.

    The cache matters: an object can appear in several groups in one run, and
    without it each appearance costs another set of UDL calls.
    """

    def __init__(self, client, spec: RunSpec, names: dict):
        self.client = client
        self.spec = spec
        self.names = names
        self.want_sources = [s for s in STATE_SOURCES if s["key"] in spec.sources]
        self._cache: dict = {}
        self.calls = 0

    def get(self, sat_no: int, mode_label: str) -> ObjectData:
        cached = self._cache.get((sat_no, mode_label))
        if cached is not None:
            return cached
        enum = DATA_MODES[mode_label]
        obj = ObjectData(sat_no=sat_no,
                         name=self.names.get(sat_no, f"OBJECT {sat_no}"),
                         colour="#4c9be8")
        for src in self.want_sources:
            svs = self.client.state_vectors(
                sat_no, self.spec.start, self.spec.end,
                source=src["udl_source"], data_mode=enum,
                default_frame=src.get("frame", "J2000"))
            self.calls += 1
            if svs:
                obj.state_series[src["key"]] = svs
        obj.elsets = self.client.elsets(sat_no, self.spec.start, self.spec.end,
                                        data_mode=enum)
        self.calls += 1
        self._cache[(sat_no, mode_label)] = obj
        return obj


def _panel_for_group(index, group, fetcher, spec, first):
    """Fetch and build one group's panel, or explain why it cannot be built."""
    objects_by_mode = {}
    for mode_label in spec.modes:
        objects_by_mode[mode_label] = {
            sat_no: fetcher.get(sat_no, mode_label) for sat_no in group["sats"]}
    reference = (group["reference"] if group["reference"] in group["sats"]
                 else group["sats"][0])
    return build_panel(index, group["name"], group["sats"], fetcher.names,
                       objects_by_mode, list(spec.modes), reference, spec.invert,
                       (spec.start, spec.end), spec.ref_epoch, first=first)


def build_report(groups: list, client, spec: RunSpec, progress=None) -> str:
    """Fetch everything the spec asks for and return the report HTML.

    A group that yields no usable data is skipped with a recorded reason rather
    than failing the whole run: one dead object should not cost you the other
    groups. If nothing at all can be built, that is an error.
    """
    if not groups:
        raise ValidationError("no groups selected")
    if len(groups) > MAX_GROUPS_PER_RUN:
        raise ValidationError(
            f"{len(groups)} groups requested; the maximum per run is "
            f"{MAX_GROUPS_PER_RUN}")
    all_sats = sorted({s for g in groups for s in g["sats"]})
    names = {rec["satNo"]: rec["name"]
             for rec in client.objects_by_satno(all_sats).values()}
    fetcher = Fetcher(client, spec, names)

    panels, skipped = [], []
    for group in groups:
        if progress:
            progress(f"{group['name']}", len(panels) + len(skipped), len(groups))
        try:
            panels.append(_panel_for_group(len(panels), group, fetcher, spec,
                                           first=not panels))
        except ComputeError as exc:
            skipped.append({"group": group["name"], "reason": str(exc)})
            event("run.group_skipped", group=group["name"], reason=str(exc))
    if not panels:
        raise ComputeError(
            "no group produced any usable data in the requested window. "
            + "; ".join(s["reason"] for s in skipped))
    event("run.panels_built", panels=len(panels), skipped=len(skipped),
          udl_calls=fetcher.calls, objects=len(all_sats))
    return render_report(panels, spec.classification)


def demo_report(spec: RunSpec) -> str:
    """A report from synthetic data: no credentials, no network. This is what
    the readiness of the whole render path is proved against in CI.

    The spec's provider selection is honoured. The demo generator produces a
    series for every configured provider, so without this filter deselecting a
    provider in the Configure tab appeared to do nothing in demo mode, which
    reads as a broken control rather than as an unused one.
    """
    from .demo import build_demo_modes
    start = spec.start or dt.datetime(2026, 6, 24)
    end = spec.end or dt.datetime(2026, 7, 1)
    wanted = set(spec.sources)
    panels = []
    for index, group in enumerate(build_demo_modes(start, end)):
        for by_sat in group["objects_by_mode"].values():
            for obj in by_sat.values():
                obj.state_series = {k: v for k, v in obj.state_series.items()
                                    if k in wanted}
        panels.append(build_panel(
            index, group["name"], group["sat_order"], group["names"],
            group["objects_by_mode"], ["REAL", "SIM"], group["reference"],
            spec.invert, (start, end), spec.ref_epoch, first=(index == 0)))
    return render_report(panels, spec.classification)
