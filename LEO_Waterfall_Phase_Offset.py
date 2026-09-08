#!/usr/bin/env python3
"""
LEO_Waterfall_Phase_Offset.py
=======================
Recreate the LeoLabs "Phase Offset (s) vs Epoch" plot for any LEO object that has
observations in the Unified Data Library (UDL).

WHAT "PHASE OFFSET" IS
----------------------
Phase offset is NOT a stored field. It is a DERIVED along-track timing error: how
many seconds, measured along the orbit, an object sits ahead of (+) or behind (-)
a fixed reference orbit. LeoLabs computes it in their viewer; the UDL holds only
the ingredients:

  * leolabs-states  -> UDL StateVector records (radar-derived state vectors)
  * spacetrack-tle  -> UDL Elset records (18 SDS / Space-Track TLEs)

This script pulls both, takes ONE common reference orbit (the waterfall anchor,
e.g. Object G's TLE), and projects every object's states and TLEs onto that
reference's along-track direction to recover the phase offset in seconds. The
reference object sits near zero (compared against itself); other objects show
their relative along-track separation from it over time. No phase-offset field
is read from the UDL because none exists.

METHOD (per data point at epoch t)
----------------------------------
  1. Propagate the REFERENCE TLE to t  -> r_ref, v_ref   (SGP4, TEME frame)
  2. Get the measured position at t    -> r_obs
        - LeoLabs state: the state vector, converted to TEME if needed
        - Space-Track TLE_i: TLE_i propagated to t (TEME)
  3. dr = r_obs - r_ref
     t_hat = v_ref / |v_ref|                (along-track unit vector)
     phase_offset = (dr . t_hat) / |v_ref|  (seconds, signed)

Frame note: SGP4 outputs TEME. UDL StateVectors are J2000/GCRF. Records carrying a
referenceFrame are converted per that tag; records without one fall back to the
per-source frame declared in STATE_SOURCES (all J2000), never silently to TEME.
LeoLabs omits referenceFrame, so that fallback is what keeps it aligned with the
labelled providers. Conversion uses astropy; --demo data is TEME so it needs none.

WHY NOT OREKIT (yet)
--------------------
For a phase offset against a reference TLE you only need SGP4 propagation plus an
along-track projection. Orekit (full force-model propagation, JVM dependency)
buys nothing here. Reach for it only if you later want to numerically propagate
the LeoLabs states themselves, ingest precise ephemerides, or do rigorous
covariance work. SGP4 + astropy is lighter and pip-only.

USAGE
-----
  Just give NORAD numbers (the first is the reference / waterfall anchor):
      python3 LEO_Waterfall_Phase_Offset.py 59884 67689 69673

  Store logins once, then never type them again:
      python3 LEO_Waterfall_Phase_Offset.py --login
  Credentials resolve in this order: environment variables, then the config file
  (~/.config/phase_offset/credentials.ini, owner-only), then an interactive
  prompt. Override the path with --config or the PHASE_OFFSET_CONFIG env var.

  Defaults: last 7 days, TLEs from Space-Track.org, auto-named HTML that opens
  in your browser. You are prompted for UDL and Space-Track.org logins at runtime
  (passwords hidden); pre-set UDL_USER/UDL_PASS and SPACETRACK_USER/SPACETRACK_PASS
  to skip the prompts.

  Optional overrides:
      --days 14                     different rolling window
      --start ... --end ...         explicit ISO 8601 UTC window
      --reference-sat 59884         anchor other than the first ID
      --tle-source udl              pull TLEs from UDL instead of Space-Track
      --invert-sign                 flip the offset sign
      --out plot.html --no-open     name the file / do not auto-open
      --demo                        synthetic data, no network
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np

try:
    from sgp4.api import Satrec, WGS72, jday
    from sgp4.exporter import export_tle
except ImportError:
    sys.exit("sgp4 is required: pip install sgp4")

MU = 398600.4418  # km^3/s^2, Earth GM


# --------------------------------------------------------------------------- #
#  Data containers
# --------------------------------------------------------------------------- #
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
    elsets: list = field(default_factory=list)          # spacetrack-tle


# UDL state-vector providers. The udl_source strings are best-guess defaults and
# MUST be verified against your UDL tenant before operational use (the exact
# source identifiers for NorthStar and KBR in particular are TBC). Adjust here.
# LeoLabs delivers J2000 but omits referenceFrame; NorthStar and KBR label it.
# All UDL state providers are J2000, so that is the per-source fallback frame.
STATE_SOURCES = [
    dict(key="leolabs",   label="LeoLabs",   udl_source="LeoLabs",   symbol="circle",      frame="J2000"),
    dict(key="northstar", label="NorthStar", udl_source="NorthStar", symbol="diamond",     frame="J2000"),
    dict(key="kbr",       label="KBR",       udl_source="KBR",        symbol="triangle-up", frame="J2000"),
]
_ST_BY_KEY = {s["key"]: s for s in STATE_SOURCES}
SRC_ORDER = [s["key"] for s in STATE_SOURCES] + ["spacetrack"]
SRC_LABEL = {**{s["key"]: s["label"] for s in STATE_SOURCES}, "spacetrack": "Space-Track"}
SRC_SYMBOL = {**{s["key"]: s["symbol"] for s in STATE_SOURCES}, "spacetrack": "square-open"}
# marker-shape CSS class for the legend / chips (shape encodes source; colour encodes object)
SRC_SHAPE = {"circle": "mk-circle", "diamond": "mk-diamond",
             "triangle-up": "mk-triangle", "square-open": "mk-square"}


# --------------------------------------------------------------------------- #
#  Frame handling
# --------------------------------------------------------------------------- #
def to_teme(sv: StateVector) -> np.ndarray:
    """Return the position in TEME (km). Convert from J2000/GCRF if needed."""
    frame = (sv.frame or "TEME").upper()
    if frame in ("TEME", ""):
        return sv.r
    # Non-TEME frame: use astropy. Imported lazily so --demo never needs it.
    try:
        from astropy import units as u
        from astropy.time import Time
        from astropy.coordinates import GCRS, TEME, CartesianRepresentation
    except ImportError:
        raise SystemExit(
            f"State frame '{sv.frame}' needs conversion to TEME. "
            "Install astropy (pip install astropy) or request TEME from the UDL."
        )
    t = Time(sv.epoch, scale="utc")
    src = GCRS(CartesianRepresentation(sv.r * u.km), obstime=t)  # treat J2000/GCRF ~ GCRS
    return src.transform_to(TEME(obstime=t)).cartesian.xyz.to(u.km).value


# --------------------------------------------------------------------------- #
#  Core phase-offset maths
# --------------------------------------------------------------------------- #
def propagate(sat: Satrec, when: dt.datetime) -> tuple[np.ndarray, np.ndarray]:
    """Propagate an SGP4 satellite to a UTC datetime -> (r, v) in TEME, km / km/s."""
    jd, fr = jday(when.year, when.month, when.day,
                  when.hour, when.minute, when.second + when.microsecond * 1e-6)
    e, r, v = sat.sgp4(jd, fr)
    if e != 0:
        raise RuntimeError(f"SGP4 error code {e} at {when.isoformat()}")
    return np.array(r), np.array(v)


def along_track_offset(r_obs: np.ndarray, r_ref: np.ndarray, v_ref: np.ndarray) -> float:
    """Signed along-track timing offset in seconds. + ahead of reference, - behind."""
    speed = np.linalg.norm(v_ref)
    t_hat = v_ref / speed
    along_km = float(np.dot(r_obs - r_ref, t_hat))
    return along_km / speed


def reference_satrec(objects, ref_sat_no, ref_epoch):
    """Build the single common reference orbit (e.g. Object G) used for the whole
    waterfall. Default: earliest TLE of the reference object; or nearest to ref_epoch."""
    try:
        ref_obj = next(o for o in objects if o.sat_no == ref_sat_no)
    except StopIteration:
        raise SystemExit(f"reference sat {ref_sat_no} is not among the loaded objects")
    if not ref_obj.elsets:
        raise SystemExit(f"reference sat {ref_sat_no} has no TLEs to anchor the reference")
    ref_obj.elsets.sort(key=lambda e: e.epoch)
    if ref_epoch is None:
        ref = ref_obj.elsets[0]
    else:
        ref = min(ref_obj.elsets, key=lambda e: abs((e.epoch - ref_epoch).total_seconds()))
    return ref.satrec()


def compute_series(obj, ref_sat, invert):
    """Offset every state source and the TLEs of `obj` against the shared reference
    orbit. Returns {source_key: [(epoch, offset_s), ...]} including 'spacetrack'.
    Only sources actually present on the object appear in the result."""
    sign = -1.0 if invert else 1.0
    out = {}
    for key, svs in obj.state_series.items():
        series = []
        for sv in sorted(svs, key=lambda s: s.epoch):
            r_ref, v_ref = propagate(ref_sat, sv.epoch)
            r_obs = to_teme(sv)
            series.append((sv.epoch, sign * along_track_offset(r_obs, r_ref, v_ref)))
        out[key] = series
    tle = []
    for els in sorted(obj.elsets, key=lambda e: e.epoch):
        r_ref, v_ref = propagate(ref_sat, els.epoch)
        r_obs, _ = propagate(els.satrec(), els.epoch)
        tle.append((els.epoch, sign * along_track_offset(r_obs, r_ref, v_ref)))
    out["spacetrack"] = tle
    return out


# --------------------------------------------------------------------------- #
#  Credentials  (env vars  ->  local config file  ->  interactive prompt)
# --------------------------------------------------------------------------- #
import configparser
import stat
from pathlib import Path


def config_path(cli=None) -> Path:
    """Resolve the credentials file path: --config, then PHASE_OFFSET_CONFIG,
    then the default under the user config directory."""
    if cli:
        return Path(cli).expanduser()
    env = os.environ.get("PHASE_OFFSET_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "phase_offset" / "credentials.ini"


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    if path.exists():
        cfg.read(path)
        if os.name == "posix":
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(f"WARN {path} is group/world accessible. "
                      f"Run: chmod 600 {path}", file=sys.stderr)
    return cfg


def save_config(path: Path, data: dict) -> Path:
    """Merge {section: {username, password}} into the config file, owner-only perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    cfg = configparser.ConfigParser(interpolation=None)
    if path.exists():
        cfg.read(path)
    for section, kv in data.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for k, v in kv.items():
            cfg.set(section, k, v)
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def credential(label, section, user_env, pass_env, cfg=None, cfg_path=None):
    """Resolve (username, password) for a service.
    Order: environment variables -> config file section -> interactive prompt.
    After a prompt, offer to save to the config file (only when interactive)."""
    user = os.environ.get(user_env)
    pwd = os.environ.get(pass_env)
    if cfg is not None and cfg.has_section(section):
        user = user or (cfg.get(section, "username", fallback="").strip() or None)
        pwd = pwd or (cfg.get(section, "password", fallback="").strip() or None)
    if user and pwd:
        return user, pwd

    print(f"\n{label} login", file=sys.stderr)
    if not user:
        user = input(f"  {label} username: ").strip()
    if not pwd:
        pwd = getpass.getpass(f"  {label} password: ")
    if not (user and pwd):
        raise SystemExit(f"{label}: username and password are both required.")

    if cfg_path is not None and sys.stdin.isatty():
        try:
            ans = input(f"  Save to {cfg_path}? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("y", "yes"):
            save_config(cfg_path, {section: {"username": user, "password": pwd}})
            print(f"  Saved (owner-only). If this sits inside a git repo, "
                  f"add it to .gitignore.", file=sys.stderr)
    return user, pwd


def do_login(cfg_path: Path):
    """Prompt once for both services and write them to the config file."""
    print(f"Configuring credentials -> {cfg_path}")
    data = {}
    for label, section in (("UDL", "udl"), ("Space-Track.org", "spacetrack")):
        print(f"\n{label}")
        u = input("  username: ").strip()
        p = getpass.getpass("  password: ")
        if u and p:
            data[section] = {"username": u, "password": p}
        else:
            print(f"  (skipped {label} - both fields needed)", file=sys.stderr)
    if data:
        save_config(cfg_path, data)
        print(f"\nSaved to {cfg_path} (owner-only, chmod 600).")
        print("Plaintext on disk: if this path is inside a git repo, add it to .gitignore.")
    else:
        print("Nothing saved.", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Satellite groups (stored as [group:<Name>] sections in the config file)
# --------------------------------------------------------------------------- #
def parse_groups(cfg) -> list:
    """Return [{name, sats, reference}] for every [group:<Name>] section."""
    groups = []
    for sect in cfg.sections():
        if not sect.lower().startswith("group:"):
            continue
        name = sect.split(":", 1)[1].strip()
        raw = cfg.get(sect, "sats", fallback="").replace(",", " ")
        sats = [int(x) for x in raw.split() if x.strip().lstrip("-").isdigit()]
        if not sats:
            continue
        ref = cfg.getint(sect, "reference", fallback=0) or sats[0]
        groups.append(dict(name=name, sats=sats, reference=ref))
    return groups


def list_groups(cfg):
    groups = parse_groups(cfg)
    if not groups:
        print("No groups defined. Add one with:\n"
              '  python3 LEO_Waterfall_Phase_Offset.py --add-group "Name" 59884 67689 69673')
        return
    print("Groups:")
    for g in groups:
        print(f"  {g['name']}: {' '.join(map(str, g['sats']))}  "
              f"(reference {g['reference']})")


def add_group(cfg_path: Path, name: str, sats: list, reference):
    ref = reference or (sats[0] if sats else 0)
    save_config(cfg_path, {f"group:{name}": {
        "sats": " ".join(map(str, sats)), "reference": str(ref)}})
    print(f"Saved group '{name}': {' '.join(map(str, sats))} "
          f"(reference {ref})\n  -> {cfg_path}")


def remove_group(cfg_path: Path, name: str):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(cfg_path)
    sect = f"group:{name}"
    if cfg.has_section(sect):
        cfg.remove_section(sect)
        with open(cfg_path, "w", encoding="utf-8") as f:
            cfg.write(f)
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        print(f"Removed group '{name}'.")
    else:
        print(f"No group named '{name}'.", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  UDL client (live path)
# --------------------------------------------------------------------------- #
class UDLClient:
    """
    Thin client over the UDL REST surface. Endpoints used:
      GET /udl/statevector   (LeoLabs state vectors)
      GET /udl/elset         (TLEs, when --tle-source udl)

    Auth: HTTP Basic. Credentials resolve from env, then the config file, then a
    prompt. Field names match the public UDL data model; adjust source/origin
    filters to your tenant's conventions.
    """

    def __init__(self, base="https://unifieddatalibrary.com", cfg=None, cfg_path=None):
        import requests
        user, pwd = credential("UDL", "udl", "UDL_USER", "UDL_PASS", cfg, cfg_path)
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, pwd)
        self.s.headers["Accept"] = "application/json"

    @staticmethod
    def _iso(d: dt.datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    def state_vectors(self, sat_no, start, end, source="LeoLabs", data_mode="REAL", default_frame="J2000"):
        params = {
            "epoch": f"{self._iso(start)}..{self._iso(end)}",
            "satNo": sat_no,
            "source": source,
            "maxResults": 5000,
        }
        if data_mode:
            params["dataMode"] = data_mode
        r = self.s.get(f"{self.base}/udl/statevector", params=params, timeout=60)
        r.raise_for_status()
        out = []
        missing = 0
        for rec in r.json():
            fr = rec.get("referenceFrame") or default_frame
            if not rec.get("referenceFrame"):
                missing += 1
            out.append(StateVector(
                epoch=_parse(rec["epoch"]),
                r=np.array([rec["xpos"], rec["ypos"], rec["zpos"]]),
                v=np.array([rec["xvel"], rec["yvel"], rec["zvel"]]),
                frame=fr,
            ))
        if missing:
            print(f"  note: {source} {missing}/{len(out)} state vectors had no "
                  f"referenceFrame; assumed {default_frame}", file=sys.stderr)
        return out

    def elsets(self, sat_no, start, end, data_mode="REAL"):
        params = {
            "epoch": f"{self._iso(start)}..{self._iso(end)}",
            "satNo": sat_no,
            "maxResults": 5000,
        }
        if data_mode:
            params["dataMode"] = data_mode
        r = self.s.get(f"{self.base}/udl/elset", params=params, timeout=60)
        r.raise_for_status()
        out = []
        for rec in r.json():
            l1, l2 = rec.get("line1"), rec.get("line2")
            if not (l1 and l2):
                l1, l2 = elset_to_tle(rec)  # build from mean elements if lines absent
            out.append(Elset(epoch=_parse(rec["epoch"]), line1=l1, line2=l2))
        return out


def _parse(s: str) -> dt.datetime:
    s = s.replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in s else s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
#  Space-Track.org client (authoritative TLE source)
# --------------------------------------------------------------------------- #
class SpaceTrackClient:
    """
    Pulls historical TLEs from Space-Track.org (class gp_history).

    Auth: form login at /ajaxauth/login, session cookie thereafter. Credentials
    come from SPACETRACK_USER / SPACETRACK_PASS if set, else a runtime prompt.
    Be considerate of the published rate limits (well under 30 requests/minute).
    """

    BASE = "https://www.space-track.org"

    def __init__(self, cfg=None, cfg_path=None):
        import requests
        user, pwd = credential("Space-Track.org", "spacetrack",
                               "SPACETRACK_USER", "SPACETRACK_PASS", cfg, cfg_path)
        self.s = requests.Session()
        r = self.s.post(f"{self.BASE}/ajaxauth/login",
                        data={"identity": user, "password": pwd}, timeout=60)
        r.raise_for_status()
        if "login" in r.url.lower() and r.text.strip().lower().startswith("<!doctype"):
            raise SystemExit("Space-Track.org login failed - check credentials.")

    @staticmethod
    def _d(d: dt.datetime) -> str:
        return d.strftime("%Y-%m-%d")

    def satcat_names(self, sat_nos):
        """Return {norad_id: OBJECT_NAME} from the Space-Track satellite catalogue.
        One batched request for all requested IDs."""
        ids = ",".join(str(s) for s in sat_nos)
        q = (f"{self.BASE}/basicspacedata/query/class/satcat/"
             f"NORAD_CAT_ID/{ids}/format/json")
        r = self.s.get(q, timeout=60)
        r.raise_for_status()
        out = {}
        for rec in r.json():
            nid = int(rec.get("NORAD_CAT_ID"))
            name = (rec.get("OBJECT_NAME") or rec.get("SATNAME") or f"OBJECT {nid}").strip()
            out[nid] = name
        return out

    def elsets(self, sat_no, start, end):
        q = (f"{self.BASE}/basicspacedata/query/class/gp_history/"
             f"NORAD_CAT_ID/{sat_no}/"
             f"EPOCH/{self._d(start)}--{self._d(end)}/"
             f"orderby/EPOCH%20asc/format/json")
        r = self.s.get(q, timeout=120)
        r.raise_for_status()
        out = []
        for rec in r.json():
            l1, l2 = rec.get("TLE_LINE1"), rec.get("TLE_LINE2")
            if l1 and l2:
                out.append(Elset(epoch=_parse(rec["EPOCH"]), line1=l1, line2=l2))
        return out


# --------------------------------------------------------------------------- #
#  UDL parsing helpers
# --------------------------------------------------------------------------- #


def elset_to_tle(rec: dict) -> tuple[str, str]:
    """Build TLE lines from a UDL Elset's mean elements when line1/line2 are absent."""
    epoch = _parse(rec["epoch"])
    yr = epoch.year % 100
    doy = (epoch - dt.datetime(epoch.year, 1, 1)).total_seconds() / 86400.0 + 1.0
    satnum = int(rec.get("satNo", 99999))

    def fmt_l1():
        s = (f"1 {satnum:05d}U 00000A   {yr:02d}{doy:012.8f} "
             f" .00000000  00000-0  00000-0 0  999")
        return s + str(_checksum(s))

    ecc = f"{rec['eccentricity']:.7f}".split(".")[1]

    def fmt_l2():
        s = (f"2 {satnum:05d} {rec['inclination']:8.4f} {rec['raan']:8.4f} "
             f"{ecc} {rec['argOfPerigee']:8.4f} {rec['meanAnomaly']:8.4f} "
             f"{rec['meanMotion']:11.8f}    1")
        return s + str(_checksum(s))

    return fmt_l1(), fmt_l2()


def _checksum(line: str) -> int:
    total = 0
    for c in line[:68]:
        if c.isdigit():
            total += int(c)
        elif c == "-":
            total += 1
    return total % 10


# --------------------------------------------------------------------------- #
#  Synthetic demo data (TEME, no astropy needed)
# --------------------------------------------------------------------------- #
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


def _make_demo_object(sat_no, name, start, end, n_rev_day, ma0, rng, sources=("leolabs", "northstar")):
    """A self-consistent synthetic object: its states AND its TLEs both track its
    own orbit, so it sits near zero against its own reference. Objects differ only
    in mean motion, so re-anchoring on any of them shifts the others coherently.
    Generates one or more state-vector providers to exercise the source controls."""
    obj = ObjectData(sat_no=sat_no, name=name, colour="#4c9be8")
    sat = _make_satrec(start, n_rev_day, 0.0008, 53.0, 120.0, 30.0, ma0, sat_no)
    window_s = (end - start).total_seconds()

    cadence = {"leolabs": 4, "northstar": 1.5, "kbr": 1.0}   # samples per day
    noise = {"leolabs": 0.05, "northstar": 0.09, "kbr": 0.12}  # km
    for key in sources:
        n = max(3, int(window_s / 86400 * cadence.get(key, 2)))
        svs = []
        for i in range(n):
            t = start + dt.timedelta(seconds=window_s * i / (n - 1))
            r, v = propagate(sat, t)
            r = r + rng.normal(0, noise.get(key, 0.08), 3)
            svs.append(StateVector(epoch=t, r=r, v=v, frame="TEME"))
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
        obj.elsets.append(Elset(epoch=t, line1=l1, line2=l2))
    return obj


def _demo_group(specs, start, end, seed):
    """specs: [(sat_no, name, drift_seconds_vs_baseline)]. Objects share an orbit
    plane; mean motion is tuned so each reaches its target offset over the window."""
    rng = np.random.default_rng(seed)
    n0 = 15.2
    window_s = (end - start).total_seconds()
    objs = []
    for sat_no, name, drift in specs:
        n = n0 * (1.0 + drift / window_s)   # (offset/n)/T = dn/n
        objs.append(_make_demo_object(sat_no, name, start, end, n, 200.0, rng))
    return objs


def build_demo(start, end):
    return _demo_group([
        (59884, "OBJECT G", 0.0),
        (67689, "PRC TEST SPACECRAFT 4", -250.0),
        (69673, "OBJECT H", -185.0),
    ], start, end, seed=42)


def build_demo_modes(start, end):
    """Two groups, each with REAL and SIM data (different drifts) so the tabbed
    layout, reference re-anchoring and the data-mode selector are all exercised."""
    def group(name, ref, real_specs, sim_specs, seed):
        real = _demo_group(real_specs, start, end, seed)
        sim = _demo_group(sim_specs, start, end, seed + 100)
        sat_order = [s for s, _, _ in real_specs]
        names = {s: n for s, n, _ in real_specs}
        return dict(name=name, sat_order=sat_order, names=names, reference=ref,
                    objects_by_mode={"REAL": {o.sat_no: o for o in real},
                                     "SIM": {o.sat_no: o for o in sim}})
    prc = group("PRC Spaceplane", 59884,
                [(59884, "OBJECT G", 0.0), (67689, "PRC TEST SPACECRAFT 4", -250.0),
                 (69673, "OBJECT H", -185.0)],
                [(59884, "OBJECT G", 0.0), (67689, "PRC TEST SPACECRAFT 4", -120.0),
                 (69673, "OBJECT H", -240.0)], 42)
    cluster = group("LEO Cluster", 40001,
                    [(40001, "CLUSTER LEAD", 0.0), (40002, "CLUSTER TRAIL", -140.0),
                     (40003, "CLUSTER TENDER", 70.0)],
                    [(40001, "CLUSTER LEAD", 0.0), (40002, "CLUSTER TRAIL", -90.0),
                     (40003, "CLUSTER TENDER", 110.0)], 7)
    return [prc, cluster]


def build_demo_groups(start, end):
    """Two synthetic groups so the tabbed layout and re-anchoring are visible."""
    cluster = _demo_group([
        (40001, "CLUSTER LEAD", 0.0),
        (40002, "CLUSTER TRAIL", -140.0),
        (40003, "CLUSTER TENDER", 70.0),
    ], start, end, seed=7)
    return [
        dict(name="PRC Spaceplane", objects=build_demo(start, end), reference=59884),
        dict(name="LEO Cluster", objects=cluster, reference=40001),
    ]


# --------------------------------------------------------------------------- #
#  Dashboard render (Plotly waterfall inside a custom mission-control shell)
# --------------------------------------------------------------------------- #
REF_COLOUR = "#C67C00"                                    # copper-amber, reference only
COOL_PALETTE = ["#4C9BE8", "#27AE60", "#E0508A", "#9B8CFF", "#22C1C3", "#F1C40F",
                "#5DD39E", "#FF8C6B", "#7FB2FF", "#D98CFF"]
# UI token -> UDL dataMode enum value
DATA_MODES = {"REAL": "REAL", "SIM": "SIMULATED", "TEST": "TEST", "EXERCISE": "EXERCISE"}


def _series_stats(series):
    """current offset, drift rate (s/day) via linear fit, and count."""
    if not series:
        return None
    ts = [e for e, _ in series]
    ys = [o for _, o in series]
    if len(ys) > 1:
        t0 = ts[0]
        xs = np.array([(t - t0).total_seconds() for t in ts])
        slope = float(np.polyfit(xs, np.array(ys), 1)[0]) * 86400.0
    else:
        slope = 0.0
    return dict(current=float(ys[-1]), drift=slope, n=len(ys))


def _sat_colours(sat_order, ref_no):
    """Fixed colour per satellite for a given reference (reference = copper)."""
    out, ci = {}, 0
    for s in sat_order:
        if s == ref_no:
            out[s] = REF_COLOUR
        else:
            out[s] = COOL_PALETTE[ci % len(COOL_PALETTE)]
            ci += 1
    return out


def _dataset(objects_by_sat, sat_order, present, names, ref_no, ref_epoch, invert, window):
    """One (mode, reference) dataset with a FIXED layout: for each sat, one trace
    per source key in `present` (state providers then Space-Track). Missing data
    yields empty arrays so trace indices stay stable for restyle."""
    ref_sat = reference_satrec(list(objects_by_sat.values()), ref_no, ref_epoch)
    colour = _sat_colours(sat_order, ref_no)
    xs, ys, colours, cards = [], [], [], []
    for s in sat_order:
        obj = objects_by_sat.get(s)
        nm = names.get(s, f"OBJECT {s}")
        series = compute_series(obj, ref_sat, invert) if obj else {}
        counts, headline = {}, None
        for key in present:
            ser = series.get(key, [])
            xs.append([o for _, o in ser])
            ys.append([e.isoformat() for e, _ in ser])
            colours.append(colour[s])
            counts[key] = len(ser)
            if headline is None and ser:
                headline = _series_stats(ser)
        primary = headline or dict(current=0.0, drift=0.0, n=0)
        cards.append(dict(norad=s, name=nm, colour=colour[s], is_ref=(s == ref_no),
                          current=primary["current"], drift=primary["drift"],
                          counts=counts, absent=all(v == 0 for v in counts.values())))
    _r0, v0 = propagate(ref_sat, window[0])
    return dict(x=xs, y=ys, colours=colours, cards=cards,
                vkms=round(float(np.linalg.norm(v0)), 3))


def _figure(sat_order, present, names, ds, div_id, first):
    """Build the Plotly figure with a fixed len(present)-traces-per-object layout,
    seeded from dataset ds. Shape encodes source; colour encodes object."""
    import plotly.graph_objects as go
    fig = go.Figure()
    traces = []
    npresent = len(present)
    for k, s in enumerate(sat_order):
        nm = names.get(s, f"OBJECT {s}")
        for j, key in enumerate(present):
            idx = k * npresent + j
            colr = ds["colours"][idx]
            symbol = SRC_SYMBOL.get(key, "circle")
            is_open = symbol.endswith("-open")
            marker = dict(symbol=symbol, size=(8 if is_open else 7), color=colr,
                          opacity=0.95,
                          line=dict(width=1.4 if is_open else 0, color=colr))
            fig.add_trace(go.Scatter(
                x=ds["x"][idx], y=ds["y"][idx], mode="markers",
                name=f"{s} {key}", marker=marker,
                hovertemplate=(f"<b>{nm}</b> · {s}<br>{SRC_LABEL.get(key, key)}<br>"
                               "%{y|%d %b %H:%M}Z<br>offset %{x:.1f} s<extra></extra>")))
            traces.append(dict(obj=s, source=key))

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#8FA0BE", family="ui-monospace,'SF Mono',Menlo,Consolas,monospace",
                  size=12),
        showlegend=False, autosize=True, margin=dict(l=64, r=24, t=16, b=52),
        hoverlabel=dict(bgcolor="#111a30", bordercolor="#385FAF",
                        font=dict(color="#E6ECF5",
                                  family="ui-monospace,Menlo,monospace", size=12)),
        xaxis=dict(title=dict(text="PHASE OFFSET  ·  seconds along-track",
                              font=dict(size=11, color="#6C7C9C")),
                   gridcolor="rgba(115,155,207,0.10)", zeroline=True,
                   zerolinecolor="rgba(198,124,0,0.55)", zerolinewidth=1.5,
                   tickfont=dict(size=11), showline=False),
        yaxis=dict(title=dict(text="EPOCH (UTC)", font=dict(size=11, color="#6C7C9C")),
                   autorange="reversed", gridcolor="rgba(115,155,207,0.10)",
                   tickfont=dict(size=11), showline=False),
        dragmode="pan",
    )
    plot_div = fig.to_html(full_html=False, include_plotlyjs=(True if first else False),
                           div_id=div_id, default_width="100%", default_height="100%",
                           config={"displaylogo": False, "responsive": True,
                                   "scrollZoom": True,
                                   "modeBarButtonsToRemove": ["select2d", "lasso2d"]})
    return plot_div, traces


def build_panel(panel_id, name, sat_order, names, objects_by_mode, mode_order,
                default_ref, invert, window, tle_source, ref_epoch, first):
    """Build one group panel across all fetched data modes. Precomputes a dataset
    per (mode, reference) so mode, reference and per-source visibility are all
    selectable in the generated HTML."""
    div_id = f"waterfall_{panel_id}"

    # Which source keys carry any data anywhere in this panel (canonical order).
    present = []
    for key in SRC_ORDER:
        has = any(
            (key != "spacetrack" and obj.state_series.get(key))
            or (key == "spacetrack" and obj.elsets)
            for by_sat in objects_by_mode.values() for obj in by_sat.values())
        if has:
            present.append(key)
    if not present:
        raise SystemExit(f"[{name}] no state or TLE data in any source.")

    mode_data = {}
    for mlabel in mode_order:
        by_sat = objects_by_mode.get(mlabel, {})
        refs, ref_sets = [], {}
        for s in sat_order:
            if s not in by_sat or not by_sat[s].elsets:
                continue  # need a TLE to anchor a reference
            try:
                ds = _dataset(by_sat, sat_order, present, names, s, ref_epoch, invert, window)
            except SystemExit:
                continue
            ref_sets[str(s)] = ds
            refs.append(dict(norad=s, name=names.get(s, f"OBJECT {s}")))
        if not ref_sets:
            continue
        dref = default_ref if str(default_ref) in ref_sets else refs[0]["norad"]
        mode_data[mlabel] = dict(refData=ref_sets, refs=refs, defaultRef=dref)

    if not mode_data:
        raise SystemExit(f"[{name}] no usable data in any requested mode.")

    first_mode = next(m for m in mode_order if m in mode_data)
    md0 = mode_data[first_mode]
    seed = md0["refData"][str(md0["defaultRef"])]
    plot_div, traces = _figure(sat_order, present, names, seed, div_id, first)
    present_meta = [dict(key=k, label=SRC_LABEL.get(k, k),
                         shape=SRC_SHAPE.get(SRC_SYMBOL.get(k, "circle"), "mk-circle"))
                    for k in present]

    return dict(
        id=panel_id, name=name, div_id=div_id, plot_div=plot_div, traces=traces,
        present=present, presentMeta=present_meta,
        modeData=mode_data, modeOrder=[m for m in mode_order if m in mode_data],
        defaultMode=first_mode, defaultRef=md0["defaultRef"], v_kms=seed["vkms"],
        window_start=window[0].strftime("%d %b %Y %H:%MZ"),
        window_end=window[1].strftime("%d %b %Y %H:%MZ"),
        source=" · ".join(SRC_LABEL.get(k, k) for k in present),
    )


def _report_css():
    return """
:root{
  --bg:#0B1020; --bg2:#0d1428; --panel:rgba(18,26,48,0.62); --panel-solid:#121a30;
  --line:rgba(115,155,207,0.16); --line-strong:rgba(115,155,207,0.30);
  --navy:#162646; --blue:#385FAF; --blue-l:#739BCF; --copper:#C67C00;
  --green:#27AE60; --text:#E6ECF5; --muted:#8FA0BE; --dim:#6C7C9C;
  --mono:ui-monospace,'SF Mono','Cascadia Mono','JetBrains Mono',Menlo,Consolas,monospace;
  --sans:'Segoe UI',system-ui,-apple-system,'Helvetica Neue',sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  background:
    radial-gradient(1200px 600px at 78% -8%, rgba(56,95,175,0.16), transparent 60%),
    radial-gradient(900px 500px at 0% 110%, rgba(198,124,0,0.08), transparent 55%),
    var(--bg);
  color:var(--text); font-family:var(--sans);
  -webkit-font-smoothing:antialiased; overflow:hidden;
}
body::before{
  content:""; position:fixed; inset:0; pointer-events:none; opacity:.35;
  background-image:linear-gradient(rgba(115,155,207,0.05) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(115,155,207,0.05) 1px,transparent 1px);
  background-size:48px 48px; mask-image:radial-gradient(circle at 50% 40%,#000 40%,transparent 92%);
}
.app{display:grid; grid-template-rows:auto auto auto 1fr auto; height:100vh}
.classif{
  font-family:var(--mono); font-size:11px; letter-spacing:.24em; text-align:center;
  padding:6px; color:var(--blue-l); background:#0a1226;
  border-bottom:1px solid var(--line); font-weight:600; text-transform:uppercase;
}
header{
  display:flex; align-items:baseline; justify-content:space-between; gap:24px;
  padding:16px 26px 12px; border-bottom:1px solid var(--line);
}
.brand .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.32em;color:var(--copper);
  text-transform:uppercase;margin:0 0 3px}
.brand h1{font-size:20px;font-weight:600;letter-spacing:.01em;margin:0;color:var(--text)}
.brand h1 b{color:var(--blue-l);font-weight:600}
.gen{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.08em;white-space:nowrap}
.tabs{display:flex;gap:2px;padding:0 20px;border-bottom:1px solid var(--line);overflow-x:auto}
.tab{appearance:none;border:0;background:transparent;color:var(--muted);cursor:pointer;
  font-family:var(--sans);font-size:13.5px;font-weight:600;letter-spacing:.01em;
  padding:13px 18px 11px;border-bottom:2px solid transparent;white-space:nowrap;
  transition:color .15s,border-color .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--copper)}
.tab .cnt{font-family:var(--mono);font-size:10px;color:var(--dim);margin-left:8px;
  background:var(--panel);padding:1px 7px;border-radius:20px}
.tab.active .cnt{color:var(--copper)}
.tab:focus-visible{outline:2px solid var(--blue-l);outline-offset:-2px}
.panels{position:relative;min-height:0}
.panel{display:none}
.panel.active{position:absolute;inset:0;display:grid;grid-template-rows:auto auto 1fr;min-height:0}
.panelmeta{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:11px 26px 9px}
.pmname{font-size:15px;font-weight:600;color:var(--text);letter-spacing:.01em}
.meta{display:flex;gap:22px;font-family:var(--mono);font-size:11.5px;text-align:right}
.meta .k{color:var(--dim);letter-spacing:.12em;text-transform:uppercase;font-size:9.5px;margin-bottom:2px}
.meta .v{color:var(--text)}
.meta .v.copper{color:var(--copper)}
.stripe{height:2px;background:linear-gradient(90deg,var(--copper),var(--blue) 30%,transparent 78%)}
main{display:grid; grid-template-columns:360px 1fr; min-height:0}
.plotwrap{position:relative; min-height:0; padding:4px 0 0 0}
.plotwrap>div{width:100%;height:100%}
.rail{border-right:1px solid var(--line); padding:16px 16px 8px; overflow-y:auto; display:flex;
  flex-direction:column; gap:14px; background:linear-gradient(180deg,rgba(18,26,48,0.35),transparent 40%)}
.rail h2{font-family:var(--mono);font-size:10px;letter-spacing:.24em;color:var(--dim);
  text-transform:uppercase;margin:0 0 8px;font-weight:600}
.seg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;padding:4px}
.seg button{appearance:none;border:0;background:transparent;color:var(--muted);cursor:pointer;
  font-family:var(--mono);font-size:11px;letter-spacing:.04em;padding:9px 4px;border-radius:8px;
  transition:background .18s,color .18s,box-shadow .18s}
.seg button:hover{color:var(--text)}
.seg button.active{color:#0B1020;background:linear-gradient(180deg,var(--blue-l),var(--blue));
  box-shadow:0 2px 10px rgba(56,95,175,0.45);font-weight:600}
.modeseg{grid-template-columns:repeat(auto-fit,minmax(0,1fr))}
.srcseg{display:flex;flex-wrap:wrap;gap:6px}
.srcchip{display:flex;align-items:center;gap:7px;appearance:none;cursor:pointer;
  font-family:var(--mono);font-size:11px;color:var(--muted);
  background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:7px 11px;
  transition:border-color .15s,color .15s,opacity .15s}
.srcchip:hover{color:var(--text);border-color:var(--line-strong)}
.srcchip:not(.active){opacity:.4}
.srcchip.active{color:var(--text);border-color:var(--line-strong)}
.srcchip:focus-visible{outline:2px solid var(--blue-l);outline-offset:2px}
.mk{display:inline-block;width:10px;height:10px;color:var(--blue-l);flex:0 0 auto}
.mk-circle{border-radius:50%;background:currentColor}
.mk-diamond{width:8px;height:8px;background:currentColor;transform:rotate(45deg)}
.mk-triangle{width:0;height:0;background:transparent;
  border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:9px solid currentColor}
.mk-square{border:1.6px solid currentColor;background:transparent}
.card.nodata{opacity:.5}
.card.nodata .big{color:var(--dim)!important}
.legendkey{display:flex;gap:16px;font-family:var(--mono);font-size:10.5px;color:var(--muted);
  padding:2px 2px 0}
.legendkey span{display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--blue-l)}
.sq{width:9px;height:9px;border:1.5px solid var(--blue-l);border-radius:2px}
.cards{display:flex;flex-direction:column;gap:9px}
.card{position:relative;border:1px solid var(--line);border-radius:13px;padding:12px 13px 12px 15px;
  background:var(--panel);backdrop-filter:blur(8px);cursor:pointer;overflow:hidden;
  transition:border-color .18s,transform .12s,opacity .18s}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.card:hover{transform:translateY(-1px);border-color:var(--line-strong)}
.card.ref{border-color:rgba(198,124,0,0.45)}
.card.off{opacity:.38}
.card .top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:9px}
.card .nm{font-size:13px;font-weight:600;color:var(--text);line-height:1.15}
.card .id{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:2px}
.refchip{font-family:var(--mono);font-size:8.5px;letter-spacing:.14em;color:#0B1020;
  background:var(--copper);padding:2px 6px;border-radius:5px;font-weight:700}
.eye{font-family:var(--mono);font-size:9px;letter-spacing:.12em;color:var(--dim);
  border:1px solid var(--line);border-radius:5px;padding:2px 6px}
.card.off .eye{color:var(--copper);border-color:rgba(198,124,0,0.4)}
.seg button:focus-visible,.card:focus-visible{outline:2px solid var(--blue-l);outline-offset:2px}
.refsel{width:100%;appearance:none;background:var(--panel);color:var(--text);
  border:1px solid var(--line);border-radius:10px;padding:10px 34px 10px 12px;
  font-family:var(--mono);font-size:12px;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--blue-l) 50%),
                   linear-gradient(135deg,var(--blue-l) 50%,transparent 50%);
  background-position:calc(100% - 17px) 55%,calc(100% - 12px) 55%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.refsel:hover{border-color:var(--line-strong)}
.refsel:focus-visible{outline:2px solid var(--blue-l);outline-offset:2px}
.refsel option{background:#121a30;color:var(--text)}
.readout{display:flex;align-items:flex-end;justify-content:space-between;gap:10px}
.big{font-family:var(--mono);font-size:26px;font-weight:600;line-height:1;letter-spacing:-0.01em;
  color:var(--text)}
.big .u{font-size:12px;color:var(--dim);margin-left:3px;font-weight:400}
.sub{font-family:var(--mono);font-size:10.5px;color:var(--muted);text-align:right;line-height:1.5}
.sub b{color:var(--blue-l);font-weight:600}
.kmline{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:9px;
  padding-top:8px;border-top:1px solid var(--line)}
.kmline b{color:var(--muted);font-weight:600}
.rel{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--panel);
  backdrop-filter:blur(8px);margin-bottom:8px}
.rel-h{font-family:var(--sans);font-size:12px;color:var(--text);display:flex;align-items:center;
  gap:6px;flex-wrap:wrap;margin-bottom:7px}
.rlink{color:var(--dim);font-family:var(--mono);font-size:12px;padding:0 2px}
.rdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto}
.rel-b{font-family:var(--mono);font-size:10.5px;color:var(--muted);display:flex;
  align-items:center;gap:8px;flex-wrap:wrap}
.rbadge{font-size:9px;letter-spacing:.12em;padding:2px 7px;border-radius:5px;font-weight:700}
.rel-cl{color:#0B1020;background:#E0952A}
.rel-sp{color:var(--blue-l);border:1px solid var(--line-strong)}
.rel-al{color:#0B1020;background:var(--green)}
.rel-st{color:var(--muted);border:1px solid var(--line)}
.foot{display:flex;justify-content:space-between;align-items:center;padding:9px 26px;
  border-top:1px solid var(--line);font-family:var(--mono);font-size:10.5px;color:var(--dim);
  letter-spacing:.04em}
.foot .r{display:flex;gap:18px}
.hint{font-family:var(--mono);font-size:10px;color:var(--dim);padding:0 2px;line-height:1.5}
@media (max-width:900px){ main{grid-template-columns:1fr} .rail{border-right:0;border-bottom:1px solid var(--line)}
  body{overflow:auto} .app{height:auto} .plotwrap{height:70vh}
  .panels{min-height:auto} .panel.active{position:static} }
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _panel_section(p, active):
    cls = "panel active" if active else "panel"
    return f"""
<section class="{cls}" data-panel="{p['id']}">
  <div class="panelmeta">
    <div class="pmname">{p['name']}</div>
    <div class="meta">
      <div><div class="k">Reference</div><div class="v copper" id="refmeta-{p['id']}"></div></div>
      <div><div class="k">Data mode</div><div class="v" id="modemeta-{p['id']}"></div></div>
      <div><div class="k">Window</div><div class="v">{p['window_start']} &rarr; {p['window_end']}</div></div>
      <div><div class="k">Sources</div><div class="v">{p['source']}</div></div>
    </div>
  </div>
  <div class="stripe"></div>
  <main>
    <aside class="rail">
      {("<div><h2>Data mode &mdash; REAL / SIM / TEST / EXERCISE</h2>"
        "<div class='seg modeseg' data-group='" + str(p['id']) + "'>"
        + "".join("<button data-mode='" + m + "'>" + m + "</button>" for m in p['modeOrder'])
        + "</div></div>") if len(p['modeOrder']) > 1 else ""}
      <div>
        <h2>Reference &mdash; re-anchor the waterfall</h2>
        <select class="refsel" data-group="{p['id']}" id="refsel-{p['id']}"
                aria-label="Reference object for {p['name']}"></select>
      </div>
      <div>
        <h2>Data sources &mdash; toggle (shape = source)</h2>
        <div class="srcseg" data-group="{p['id']}">
          {"".join(f'<button class="srcchip active" data-src="{m["key"]}"><span class="mk {m["shape"]}"></span>{m["label"]}</button>' for m in p['presentMeta'])}
        </div>
      </div>
      <div>
        <h2>Objects &mdash; tap to isolate</h2>
        <div class="cards" id="cards-{p['id']}"></div>
      </div>
      <div>
        <h2>Relative motion &mdash; object to object</h2>
        <div id="rel-{p['id']}"></div>
      </div>
      <p class="hint"><b style="color:var(--muted)">Offset (s)</b> is along-track timing: how far apart along the orbit, expressed as travel time. &minus;120 s means the object passes a given point about 120 s after the reference &mdash; trailing by roughly 120 s, near {p['v_kms']} km/s that is about {round(p['v_kms'])} km per second of offset. <b style="color:var(--muted)">Drift (s/day)</b> is how fast that gap is changing. <b style="color:var(--muted)">Closing / separating</b> combines the two: it is along-track timing only, not a conjunction &mdash; radial and cross-track separation are not shown here. Drag to pan, scroll to zoom, double-click to reset.</p>
    </aside>
    <div class="plotwrap">{p['plot_div']}</div>
  </main>
</section>"""


def render_report(panels, out_path, classification):
    """Assemble the multi-group tabbed report into one self-contained HTML file."""
    import json
    css = _report_css()
    generated = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y %H:%M:%SZ")

    tabs = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-tab="{p["id"]}">'
        f'{p["name"]}<span class="cnt">{len(p["traces"]) // 2}</span></button>'
        for i, p in enumerate(panels))
    sections = "".join(_panel_section(p, i == 0) for i, p in enumerate(panels))
    groups = json.dumps([dict(id=p["id"], divId=p["div_id"], traces=p["traces"],
                              present=p["present"],
                              modeData=p["modeData"], modeOrder=p["modeOrder"],
                              defaultMode=p["defaultMode"], defaultRef=p["defaultRef"])
                         for p in panels])
    srclbl = json.dumps(SRC_LABEL)

    body = f"""
<div class="app">
  <div class="classif">{classification.upper()}</div>
  <header>
    <div class="brand">
      <p class="eyebrow">Bluestaq Limited · Space Domain Awareness</p>
      <h1>Phase Offset Waterfall <b>&mdash; relative along-track drift</b></h1>
    </div>
    <div class="gen">GENERATED {generated}</div>
  </header>
  <nav class="tabs">{tabs}</nav>
  <div class="panels">{sections}</div>
  <div class="foot">
    <span>BLUESTAQ LIMITED &nbsp;·&nbsp; MISSION CRITICAL SOLUTIONS</span>
    <span class="r"><span>{len(panels)} GROUP{'S' if len(panels) != 1 else ''}</span></span>
  </div>
</div>
<script>
const GROUPS = {groups};
const SRCLBL = {srclbl};
const ST = {{}};
GROUPS.forEach(g => {{
  ST[g.id] = {{ sources:new Set(g.present), hidden:new Set(),
                mode:g.defaultMode, ref:String(g.defaultRef) }};
  g.cards = []; g.vkms = 1;
}});

function fmt(n, dp=1){{ const s = n>=0 ? "+" : ""; return s + n.toFixed(dp); }}
function km(sec, v){{ return Math.abs(sec)*v; }}
function panelEl(id){{ return document.querySelector('.panel[data-panel="'+id+'"]'); }}

function refOptions(g){{
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  const sel = document.getElementById("refsel-"+g.id);
  if(!sel) return;
  sel.innerHTML = md.refs.map(r =>
    `<option value="${{r.norad}}"${{String(r.norad)===st.ref?" selected":""}}>${{r.name}} · ${{r.norad}}</option>`
  ).join("");
}}

function applyData(g){{
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  if(!md.refData[st.ref]) st.ref = String(md.defaultRef);   // ref absent in this mode
  const rd = md.refData[st.ref];
  g.cards = rd.cards; g.vkms = rd.vkms;
  if(window.Plotly)
    Plotly.restyle(g.divId, {{x: rd.x, y: rd.y, "marker.color": rd.colours}});
  const refc = rd.cards.find(c => c.is_ref);
  const rm = document.getElementById("refmeta-"+g.id);
  if(refc && rm) rm.textContent = refc.name + " · " + refc.norad;
  const mm = document.getElementById("modemeta-"+g.id);
  if(mm) mm.textContent = st.mode;
  const sel = document.getElementById("refsel-"+g.id);
  if(sel && sel.value !== st.ref) sel.value = st.ref;
  panelEl(g.id).querySelectorAll(".modeseg button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode===st.mode));
  buildCards(g); buildRelative(g); apply(g);
}}

function setRef(g, refNo){{ ST[g.id].ref = String(refNo); applyData(g); }}
function setMode(g, mode){{ ST[g.id].mode = mode; refOptions(g); applyData(g); }}

function buildCards(g){{
  const wrap = document.getElementById("cards-"+g.id);
  wrap.innerHTML = g.cards.map(c => `
    <div class="card ${{c.is_ref?'ref':''}} ${{c.absent?'nodata':''}}" data-obj="${{c.norad}}" style="--c:${{c.colour}}"
         role="button" tabindex="0" aria-label="${{c.name}} ${{c.norad}}, tap to isolate">
      <div class="top">
        <div><div class="nm">${{c.name}}</div><div class="id">NORAD ${{c.norad}}</div></div>
        ${{c.is_ref ? '<span class="refchip">REF</span>' : '<span class="eye">SHOWN</span>'}}
      </div>
      <div class="readout">
        <div class="big" style="color:${{c.colour}}">${{c.absent?'&mdash;':fmt(c.current)}}<span class="u">${{c.absent?'':'s'}}</span></div>
        <div class="sub">drift <b>${{c.absent?'&mdash;':fmt(c.drift)}}</b> s/day<br>${{
          Object.entries(c.counts).filter(e=>e[1]>0).map(e=>SRCLBL[e[0]]+' '+e[1]).join(' · ') || 'no data'
        }}</div>
      </div>
      <div class="kmline">${{c.absent
        ? 'no data in this mode'
        : (c.is_ref
          ? 'reference datum &mdash; all offsets measured from here'
          : (c.current<0?'trails':'leads') + ' reference by <b>~'+km(c.current,g.vkms).toFixed(0)+' km</b> along-track')}}</div>
    </div>`).join("");
  wrap.querySelectorAll(".card").forEach(el => {{
    el.addEventListener("click", () => toggleObj(g, +el.dataset.obj));
    el.addEventListener("keydown", e => {{
      if(e.key==="Enter"||e.key===" "){{ e.preventDefault(); toggleObj(g, +el.dataset.obj); }}
    }});
  }});
}}

function buildRelative(g){{
  const box = document.getElementById("rel-"+g.id);
  const rows = [];
  for(let i=0;i<g.cards.length;i++) for(let j=i+1;j<g.cards.length;j++){{
    const a=g.cards[i], b=g.cards[j];
    const gap = a.current - b.current;
    const rel = a.drift - b.drift;
    const rate = Math.abs(rel);
    let state, cls, eta="";
    if(Math.abs(gap) < 1){{ state="ALIGNED"; cls="rel-al"; }}
    else if(rate < 0.05){{ state="STEADY"; cls="rel-st"; }}
    else if(Math.sign(gap) === -Math.sign(rel)){{
      state="CLOSING"; cls="rel-cl"; eta = " · ~"+(Math.abs(gap)/rate).toFixed(0)+" d to align";
    }} else {{ state="SEPARATING"; cls="rel-sp"; }}
    const link = state==="CLOSING" ? "&rarr;&larr;" : state==="SEPARATING" ? "&larr;&nbsp;&rarr;" : "&mdash;";
    const rateStr = rate>=0.05 ? rate.toFixed(1)+" s/day · " : "";
    rows.push(`<div class="rel">
      <div class="rel-h"><span class="rdot" style="background:${{a.colour}}"></span>${{a.name}}
        <span class="rlink">${{link}}</span>
        <span class="rdot" style="background:${{b.colour}}"></span>${{b.name}}</div>
      <div class="rel-b"><span class="rbadge ${{cls}}">${{state}}</span>
        ${{rateStr}}gap ${{Math.abs(gap).toFixed(0)}} s (~${{km(gap,g.vkms).toFixed(0)}} km)${{eta}}</div>
    </div>`);
  }}
  box.innerHTML = rows.length ? rows.join("") :
    '<p class="hint">Add two or more objects to see relative motion.</p>';
}}

function toggleObj(g, norad){{
  const h = ST[g.id].hidden;
  if(h.has(norad)) h.delete(norad); else h.add(norad);
  apply(g);
}}

function apply(g){{
  const st = ST[g.id];
  const vis = g.traces.map(t => st.sources.has(t.source) && !st.hidden.has(t.obj));
  if(window.Plotly) Plotly.restyle(g.divId, {{visible: vis}});
  const panel = panelEl(g.id);
  panel.querySelectorAll(".srcseg .srcchip").forEach(b =>
    b.classList.toggle("active", st.sources.has(b.dataset.src)));
  panel.querySelectorAll(".cards .card").forEach(el => {{
    const off = st.hidden.has(+el.dataset.obj);
    el.classList.toggle("off", off);
    const eye = el.querySelector(".eye");
    if(eye) eye.textContent = off ? "HIDDEN" : "SHOWN";
  }});
}}

function showTab(id){{
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", +p.dataset.panel===id));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", +t.dataset.tab===id));
  const g = GROUPS.find(x => x.id===id);
  if(g && window.Plotly) Plotly.Plots.resize(g.divId);
}}

function init(){{
  GROUPS.forEach(g => {{
    refOptions(g);
    panelEl(g.id).querySelectorAll(".srcseg .srcchip").forEach(b =>
      b.addEventListener("click", () => {{
        const st = ST[g.id];
        if(st.sources.has(b.dataset.src)) st.sources.delete(b.dataset.src);
        else st.sources.add(b.dataset.src);
        apply(g);
      }}));
    panelEl(g.id).querySelectorAll(".modeseg button").forEach(b =>
      b.addEventListener("click", () => setMode(g, b.dataset.mode)));
    const sel = document.getElementById("refsel-"+g.id);
    if(sel) sel.addEventListener("change", () => setRef(g, sel.value));
    applyData(g);
  }});
  document.querySelectorAll(".tab").forEach(t =>
    t.addEventListener("click", () => showTab(+t.dataset.tab)));
  if(GROUPS.length) showTab(GROUPS[0].id);
}}
window.addEventListener("load", init);
</script>
"""
    html = ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Phase Offset Waterfall</title><style>" + css + "</style></head><body>"
            + body + "</body></html>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path



# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _dt(s: str) -> dt.datetime:
    return _parse(s)


def main():
    ap = argparse.ArgumentParser(
        description="LeoLabs-style phase-offset waterfall from UDL + Space-Track data.",
        epilog="Examples:\n"
               "  python3 LEO_Waterfall_Phase_Offset.py 59884 67689 69673\n"
               '  python3 LEO_Waterfall_Phase_Offset.py --add-group "PRC Spaceplane" 59884 67689 69673\n'
               "  python3 LEO_Waterfall_Phase_Offset.py            (renders every saved group as a tab)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sats", nargs="*", type=int,
                    help="NORAD numbers (first is the reference). Omit to render saved groups.")
    ap.add_argument("--days", type=int, default=7,
                    help="window length in days ending now (default: 7)")
    ap.add_argument("--start", type=_dt, help="override window start (ISO 8601 UTC)")
    ap.add_argument("--end", type=_dt, help="override window end (ISO 8601 UTC)")
    ap.add_argument("--reference-sat", type=int, default=None,
                    help="waterfall anchor for an ad-hoc selection or a new group")
    ap.add_argument("--title", default="Selection",
                    help="tab title when plotting an ad-hoc list of NORAD numbers")
    ap.add_argument("--tle-source", choices=["spacetrack", "udl"], default="spacetrack",
                    help="source of the spacetrack-tle series (default: spacetrack)")
    ap.add_argument("--data-mode", nargs="+", choices=list(DATA_MODES.keys()),
                    default=["REAL"], metavar="MODE",
                    help="UDL data modes to fetch and embed (REAL SIM TEST EXERCISE). "
                         "Default REAL. Extra modes become selectable in the output.")
    ap.add_argument("--sources", nargs="+", choices=[s["key"] for s in STATE_SOURCES],
                    default=[s["key"] for s in STATE_SOURCES], metavar="SRC",
                    help="UDL state-vector providers to ingest (leolabs northstar kbr). "
                         "Default all. Each present provider is toggleable in the output.")
    ap.add_argument("--ref-epoch", type=_dt, default=None,
                    help="reference epoch (default: earliest TLE of the reference object)")
    ap.add_argument("--invert-sign", action="store_true", help="flip the offset sign")
    ap.add_argument("--classification", default="UNCLASSIFIED",
                    help="banner classification (set per your marking policy)")
    ap.add_argument("--out", default=None, help="output HTML path (default: auto-named)")
    ap.add_argument("--no-open", action="store_true", help="do not open the report in a browser")
    ap.add_argument("--config", default=None,
                    help="config/credentials file (default: ~/.config/phase_offset/credentials.ini)")
    ap.add_argument("--login", action="store_true",
                    help="store UDL and Space-Track credentials, then exit")
    ap.add_argument("--add-group", metavar="NAME", default=None,
                    help="save the given NORAD numbers as a named group, then exit")
    ap.add_argument("--group", action="append", default=[], metavar="NAME",
                    help="render only this saved group (repeatable)")
    ap.add_argument("--list-groups", action="store_true", help="list saved groups, then exit")
    ap.add_argument("--remove-group", metavar="NAME", default=None,
                    help="delete a saved group, then exit")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    args = ap.parse_args()

    cfg_path = config_path(args.config)
    cfg = load_config(cfg_path)

    # --- management commands (act, then exit) ---
    if args.login:
        do_login(cfg_path); return
    if args.list_groups:
        list_groups(cfg); return
    if args.remove_group:
        remove_group(cfg_path, args.remove_group); return
    if args.add_group:
        if not args.sats:
            ap.error('--add-group needs NORAD numbers, e.g. --add-group "PRC" 59884 67689')
        add_group(cfg_path, args.add_group, args.sats, args.reference_sat); return

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    end = args.end or now
    start = args.start or (end - dt.timedelta(days=args.days))

    panels = []
    modes = list(dict.fromkeys(args.data_mode))   # de-dup, preserve order

    if args.demo:
        start = args.start or _dt("2026-06-24T00:00:00Z")
        end = args.end or _dt("2026-07-01T00:00:00Z")
        modes = ["REAL", "SIM"]
        for pid, grp in enumerate(build_demo_modes(start, end)):
            panels.append(build_panel(
                pid, grp["name"], grp["sat_order"], grp["names"],
                grp["objects_by_mode"], ["REAL", "SIM"], grp["reference"],
                args.invert_sign, (start, end), args.tle_source, args.ref_epoch,
                first=(pid == 0)))
    else:
        # Resolve which group specs to render: ad-hoc list, named groups, or all saved.
        if args.sats:
            specs = [dict(name=args.title, sats=args.sats,
                          reference=args.reference_sat or args.sats[0])]
        else:
            groups = parse_groups(cfg)
            if args.group:
                wanted = {g.lower() for g in args.group}
                groups = [g for g in groups if g["name"].lower() in wanted]
                if not groups:
                    ap.error("no saved group matched --group. Try --list-groups.")
            if not groups:
                ap.error("no NORAD numbers given and no saved groups. Add IDs on the "
                         'command line, or create a group with --add-group "Name" <ids...>.')
            specs = groups

        udl = UDLClient(cfg=cfg, cfg_path=cfg_path)
        st = SpaceTrackClient(cfg=cfg, cfg_path=cfg_path) if args.tle_source == "spacetrack" else None
        all_sats = sorted({s for spec in specs for s in spec["sats"]})
        names = st.satcat_names(all_sats) if st else {}

        cache = {}
        want_sources = [s for s in STATE_SOURCES if s["key"] in args.sources]
        def fetch(sat_no, mlabel):
            key = (sat_no, mlabel)
            if key not in cache:
                enum = DATA_MODES[mlabel]
                obj = ObjectData(sat_no=sat_no,
                                 name=names.get(sat_no, f"OBJECT {sat_no}"),
                                 colour="#4c9be8")
                for src in want_sources:
                    svs = udl.state_vectors(sat_no, start, end,
                                            source=src["udl_source"], data_mode=enum,
                                            default_frame=src.get("frame", "J2000"))
                    if svs:
                        obj.state_series[src["key"]] = svs
                if mlabel == "REAL" and st is not None:
                    obj.elsets = st.elsets(sat_no, start, end)       # real catalogue
                else:
                    obj.elsets = udl.elsets(sat_no, start, end, data_mode=enum)
                cache[key] = obj
            return cache[key]

        for pid, spec in enumerate(specs):
            print(f"[{spec['name']}]")
            objects_by_mode = {}
            for mlabel in modes:
                by_sat = {}
                for sat_no in spec["sats"]:
                    obj = fetch(sat_no, mlabel)
                    by_sat[sat_no] = obj
                    counts = " ".join(f"{k}:{len(v)}" for k, v in obj.state_series.items())
                    print(f"  {mlabel} {sat_no} {obj.name}: {counts} · "
                          f"{len(obj.elsets)} TLEs")
                objects_by_mode[mlabel] = by_sat
            ref_no = spec["reference"] if spec["reference"] in spec["sats"] else spec["sats"][0]
            panels.append(build_panel(
                pid, spec["name"], spec["sats"], names, objects_by_mode, modes,
                ref_no, args.invert_sign, (start, end), args.tle_source,
                args.ref_epoch, first=(pid == 0)))

    out_path = args.out or f"phase_offset_{end:%Y%m%d}.html"
    out = render_report(panels, out_path, args.classification)
    print(f"Wrote {out}  ({len(panels)} group(s), modes {'+'.join(modes)}, "
          f"window {start:%Y-%m-%d} to {end:%Y-%m-%d})")

    if not args.no_open:
        import webbrowser
        webbrowser.open(f"file://{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
