"""Data containers and source constants.

Lifted from LEO_Waterfall_Phase_Offset.py without behavioural change.

Two kinds of series are plotted and they must not be confused, because an
earlier version of this file did confuse them.

  * **State-vector series**, one per provider, from ``/udl/statevector``
    filtered by ``source``. These are measured positions. Five providers are
    configured in STATE_SOURCES, and Space-Track is one of them.
  * **The element-set series**, keyed ELSET_KEY, derived from
    ``/udl/elset``. This is not a provider: it is what you get by propagating
    each two-line element set to its own epoch, and it is also where the
    reference orbit that anchors the whole waterfall comes from.

The element-set key used to be ``"spacetrack"``, on the reasoning that the
records the UDL serves on /udl/elset are 18th Space Defense Squadron (18 SDS)
two-line element sets. That was accurate but it took the name that a real
state-vector provider needs, and it made the Configure tab's provider list
disagree with the report's legend: the tab offered state providers only while
the legend also carried the element-set series. The key is now ``elset``,
labelled "Element sets", and "Space-Track" means the state-vector provider.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
from sgp4.api import Satrec

@dataclass
class StateVector:
    epoch: dt.datetime          # UTC
    r: np.ndarray               # km, 3-vector
    v: np.ndarray               # km/s, 3-vector
    frame: str = "TEME"         # UDL referenceFrame


@dataclass
class Elset:
    epoch: dt.datetime
    line1: str
    line2: str

    def satrec(self) -> Satrec:
        return Satrec.twoline2rv(self.line1, self.line2)


@dataclass
class ObjectData:
    sat_no: int
    name: str
    colour: str
    state_series: dict = field(default_factory=dict)   # source key -> [StateVector]
    elsets: list = field(default_factory=list)         # TLE series


# UDL state-vector providers, queried as /udl/statevector?source=<udl_source>.
#
# VERIFY THE udl_source STRINGS AGAINST YOUR TENANT. They are the names the
# providers are commonly known by, not values read back from a live UDL, and a
# tenant that spells one differently returns an empty series rather than an
# error: the provider simply never appears in the report. The original script
# carried this caveat for NorthStar and KBR; it applies to all five.
#
# GET /api/sources/probe answers the question directly by asking the UDL for one
# record per source, so the guesses do not have to be taken on trust.
#
# Frames: LeoLabs delivers J2000 but omits referenceFrame, and the others label
# it. J2000 is the per-source fallback for every provider, applied only when a
# record carries no referenceFrame of its own, and every such assumption is
# recorded as an audit event because a wrong frame means wrong offsets.
#
# Symbols: shape encodes the source, colour encodes the object. Filled shapes
# are measured state vectors; the one open shape is the element-set series,
# which is derived rather than measured.
STATE_SOURCES = [
    dict(key="leolabs",    label="LeoLabs",     udl_source="LeoLabs",    symbol="circle",      frame="J2000"),
    dict(key="northstar",  label="NorthStar",   udl_source="NorthStar",  symbol="diamond",     frame="J2000"),
    dict(key="kbr",        label="KBR",         udl_source="KBR",        symbol="triangle-up", frame="J2000"),
    dict(key="ppec",       label="PPEC",        udl_source="PPEC",       symbol="cross",       frame="J2000"),
    dict(key="spacetrack", label="Space-Track", udl_source="Space-Track", symbol="x",          frame="J2000"),
]
STATE_SOURCE_KEYS = [s["key"] for s in STATE_SOURCES]

# The element-set series. Not a provider: it is every two-line element set in
# the window propagated to its own epoch, and the source of the reference orbit.
ELSET_KEY = "elset"
ELSET_LABEL = "Element sets"

SRC_ORDER = STATE_SOURCE_KEYS + [ELSET_KEY]
SRC_LABEL = {**{s["key"]: s["label"] for s in STATE_SOURCES}, ELSET_KEY: ELSET_LABEL}
SRC_SYMBOL = {**{s["key"]: s["symbol"] for s in STATE_SOURCES}, ELSET_KEY: "square-open"}
# marker-shape CSS class for the legend / chips (shape encodes source; colour encodes object)
SRC_SHAPE = {"circle": "mk-circle", "diamond": "mk-diamond",
             "triangle-up": "mk-triangle", "square-open": "mk-square",
             "cross": "mk-cross", "x": "mk-ex"}

# UDL dataMode enum values, keyed by the label used on the wire and in the UI.
DATA_MODES = {"REAL": "REAL", "SIM": "SIMULATED", "TEST": "TEST", "EXERCISE": "EXERCISE"}
