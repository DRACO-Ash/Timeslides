"""Data containers and source constants.

Lifted from LEO_Waterfall_Phase_Offset.py without behavioural change.

A note on the ``spacetrack`` series key, because it now looks wrong and is not.
The key names the TLE-derived series. TLEs are fetched from the UDL elset
endpoint, but the records the UDL serves there are 18th Space Defense Squadron
(18 SDS) / Space-Track two-line element sets. The provenance label is therefore
still accurate, and keeping the key means the report's legend, marker shapes and
client-side code are untouched.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
from sgp4.api import Satrec

MU = 398600.4418  # km^3/s^2, Earth GM


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


# UDL state-vector providers. The udl_source strings are best-guess defaults and
# MUST be verified against your UDL tenant before operational use (the exact
# source identifiers for NorthStar and KBR in particular are TBC). Adjust here.
# LeoLabs delivers J2000 but omits referenceFrame; NorthStar and KBR label it.
# All UDL state providers are J2000, so that is the per-source fallback frame.
STATE_SOURCES = [
    dict(key="leolabs",   label="LeoLabs",   udl_source="LeoLabs",   symbol="circle",      frame="J2000"),
    dict(key="northstar", label="NorthStar", udl_source="NorthStar", symbol="diamond",     frame="J2000"),
    dict(key="kbr",       label="KBR",       udl_source="KBR",       symbol="triangle-up", frame="J2000"),
]
STATE_SOURCE_KEYS = [s["key"] for s in STATE_SOURCES]
SRC_ORDER = STATE_SOURCE_KEYS + ["spacetrack"]
SRC_LABEL = {**{s["key"]: s["label"] for s in STATE_SOURCES}, "spacetrack": "Space-Track"}
SRC_SYMBOL = {**{s["key"]: s["symbol"] for s in STATE_SOURCES}, "spacetrack": "square-open"}
# marker-shape CSS class for the legend / chips (shape encodes source; colour encodes object)
SRC_SHAPE = {"circle": "mk-circle", "diamond": "mk-diamond",
             "triangle-up": "mk-triangle", "square-open": "mk-square"}

# UDL dataMode enum values, keyed by the label used on the wire and in the UI.
DATA_MODES = {"REAL": "REAL", "SIM": "SIMULATED", "TEST": "TEST", "EXERCISE": "EXERCISE"}
