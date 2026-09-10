"""Synthetic data, in TEME so no frame conversion is needed.

Lifted from LEO_Waterfall_Phase_Offset.py unchanged. In the original this
served the `--demo` flag. Here it does that and earns its keep twice over as
the test fixture set: it produces self-consistent objects whose state vectors
and TLEs both track their own orbit, which is exactly what is needed to test
the maths and the renderer with no network and no credentials.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
from sgp4.api import WGS72, Satrec, jday
from sgp4.exporter import export_tle

from .models import (SRC_LABEL, STATE_SOURCE_KEYS, Elset, ObjectData,
                     StateVector)

from .physics import propagate
# The synthetic objects, named once. The same three appear in the REAL and SIM
# sets of the demo group and in the fixtures, so the strings were repeated.
OBJECT_G = "OBJECT G"
OBJECT_H = "OBJECT H"
PRC_TEST_4 = "PRC TEST SPACECRAFT 4"
CLUSTER_LEAD = "CLUSTER LEAD"
CLUSTER_TRAIL = "CLUSTER TRAIL"
CLUSTER_TENDER = "CLUSTER TENDER"


def _make_satrec(epoch, n_rev_day, ecc, inc_deg, raan_deg, argp_deg, ma_deg, satnum):
    sat = Satrec()
    jd, fr = jday(epoch.year, epoch.month, epoch.day, epoch.hour, epoch.minute, epoch.second)
    sat.sgp4init(
        WGS72, "i", satnum, (jd + fr) - 2433281.5,
        0.0, 0.0, 0.0,            # bstar, ndot, nddot
        ecc, math.radians(argp_deg),
        math.radians(inc_deg), math.radians(ma_deg),
        n_rev_day * 2 * math.pi / 1440.0,   # mean motion, rad/min
        math.radians(raan_deg),
    )
    return sat


def _tle_lines(sat) -> tuple[str, str]:
    return export_tle(sat)


def _make_demo_object(sat_no, name, start, end, n_rev_day, ma0, rng, sources=None):
    """A self-consistent synthetic object: its states AND its TLEs both track its
    own orbit, so it sits near zero against its own reference. Objects differ only
    in mean motion, so re-anchoring on any of them shifts the others coherently.
    Generates a series for every configured state-vector provider so the source
    controls, the marker shapes and the legend are all exercised without a
    tenant. Cadence and noise differ per provider so the traces are
    distinguishable on the plot rather than sitting on top of each other."""
    sources = STATE_SOURCE_KEYS if sources is None else sources
    obj = ObjectData(sat_no=sat_no, name=name, colour="#4c9be8")
    sat = _make_satrec(start, n_rev_day, 0.0008, 53.0, 120.0, 30.0, ma0, sat_no)
    window_s = (end - start).total_seconds()

    # samples per day, and positional noise in km
    cadence = {"leolabs": 4, "northstar": 1.5, "kbr": 1.0, "ppec": 2.0,
               "spacetrack": 1.2}
    noise = {"leolabs": 0.05, "northstar": 0.09, "kbr": 0.12, "ppec": 0.07,
             "spacetrack": 0.15}
    for key in sources:
        n = max(3, int(window_s / 86400 * cadence.get(key, 2)))
        svs = []
        for i in range(n):
            t = start + dt.timedelta(seconds=window_s * i / (n - 1))
            r, v = propagate(sat, t)
            r = r + rng.normal(0, noise.get(key, 0.08), 3)
            svs.append(StateVector(epoch=t, r=r, v=v, frame="TEME",
                                   source=SRC_LABEL.get(key, key),
                                   created=t.strftime("%Y-%m-%dT%H:%M:%SZ")))
        obj.state_series[key] = svs

    n_rad_min = n_rev_day * 2 * math.pi / 1440.0
    n_tle = max(3, int(window_s / 86400 * 1.5))
    for i in range(n_tle):
        t = start + dt.timedelta(seconds=window_s * i / (n_tle - 1))
        minutes = (t - start).total_seconds() / 60.0
        ma_t = (ma0 + math.degrees(n_rad_min * minutes)) % 360.0
        s = _make_satrec(t, n_rev_day, 0.0008, 53.0, 120.0, 30.0,
                         ma_t + rng.normal(0, 0.002), sat_no)
        l1, l2 = _tle_lines(s)
        # Two originators, alternating, because that is what the real feed
        # looks like: /udl/elset is not filtered by source, so a tenant holding
        # element sets from more than one producer returns them all in one
        # series. Demo mode showing a single source would misrepresent the one
        # series whose provenance varies per point.
        obj.elsets.append(Elset(epoch=t, line1=l1, line2=l2,
                                source=ELSET_ORIGINATORS[i % len(ELSET_ORIGINATORS)],
                                created=t.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return obj


# The originators demo element sets are attributed to. 18 SDS produces the
# general perturbations catalogue that Space-Track distributes, so a tenant
# commonly holds both labels for the same lineage; showing two makes the
# per-point provenance in the tooltip mean something.
ELSET_ORIGINATORS = ("18 SDS", "Space-Track")


def _demo_group(specs, start, end, seed):
    """specs: [(sat_no, name, drift_seconds_vs_baseline)]. Objects share an orbit
    plane; mean motion is tuned so each reaches its target offset over the window."""
    rng = np.random.default_rng(seed)
    n0 = 15.2
    window_s = (end - start).total_seconds()
    objs = []
    for sat_no, name, drift in specs:
        # A fractional change in mean motion of drift/window_s produces the
        # target along-track offset by the end of the window.
        n = n0 * (1.0 + drift / window_s)
        objs.append(_make_demo_object(sat_no, name, start, end, n, 200.0, rng))
    return objs


def build_demo(start, end):
    return _demo_group([
        (59884, OBJECT_G, 0.0),
        (67689, PRC_TEST_4, -250.0),
        (69673, OBJECT_H, -185.0),
    ], start, end, seed=42)


def build_demo_modes(start, end):
    """Two groups, each with REAL and SIM data (different drifts) so the tabbed
    layout, reference re-anchoring and the data-mode selector are all exercised."""
    def group(name, ref, real_specs, sim_specs, seed):
        real = _demo_group(real_specs, start, end, seed)
        sim = _demo_group(sim_specs, start, end, seed + 100)
        sat_order = [s for s, _, _ in real_specs]
        names = {s: n for s, n, _ in real_specs}
        return {
            "name": name,
            "sat_order": sat_order,
            "names": names,
            "reference": ref,
            "objects_by_mode": {"REAL": {o.sat_no: o for o in real},
                                     "SIM": {o.sat_no: o for o in sim}}}
    prc = group("PRC Spaceplane", 59884,
                [(59884, OBJECT_G, 0.0), (67689, PRC_TEST_4, -250.0),
                 (69673, OBJECT_H, -185.0)],
                [(59884, OBJECT_G, 0.0), (67689, PRC_TEST_4, -120.0),
                 (69673, OBJECT_H, -240.0)], 42)
    cluster = group("LEO Cluster", 40001,
                    [(40001, CLUSTER_LEAD, 0.0), (40002, CLUSTER_TRAIL, -140.0),
                     (40003, CLUSTER_TENDER, 70.0)],
                    [(40001, CLUSTER_LEAD, 0.0), (40002, CLUSTER_TRAIL, -90.0),
                     (40003, CLUSTER_TENDER, 110.0)], 7)
    return [prc, cluster]
