"""Unified Data Library client. The only external dependency this app has.

Space-Track is gone. It used to supply the default TLE series and the object
names; both now come from the UDL, which removes a second credential pair, a
second outage surface and a published 30-requests-per-minute limit to police.
The element sets the UDL serves on /udl/elset are 18th Space Defense Squadron
(18 SDS) two-line element sets, which is what the Space-Track path was fetching
anyway, so the series and its provenance label are unchanged.

Endpoints used:
  GET /udl/statevector   state vectors, per provider
  GET /udl/elset         element sets
  GET /udl/onorbit       the on-orbit object catalogue, for the picker

VERIFY BEFORE OPERATIONAL USE. The /udl/onorbit field names and the provider
source strings in models.STATE_SOURCES are taken from the public UDL data model,
not from a call against your tenant. The original script carried the same
caveat for NorthStar and KBR. Everything a tenant might disagree about is
gathered into ONORBIT_FIELDS and STATE_SOURCES so it is one edit, and a field
that is missing degrades to a placeholder name rather than raising.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from .audit import event
from .errors import UpstreamError
from .models import (DATA_MODES, STATE_SOURCES, UNATTRIBUTED, Elset,
                     StateVector)
from .ratelimit import TokenBucket

# Provenance fields, on both the elset and state-vector records. Aliased the
# same way as the onorbit fields and for the same reason: these names come from
# the public UDL data model, not from a call against your tenant.
#
# `source` is the originator of the record. `origin` is the system that
# delivered it, which is often the same and sometimes not. `created` is the
# feed's own ingest stamp, used only to resolve a same-epoch conflict the same
# way on every run.
PROVENANCE_FIELDS = {
    "source": ("source", "sourceDL", "dataSource", "origNetwork", "origin",
               "originator", "provider"),
    "origin": ("origin", "origNetwork", "sourceDL"),
    "created": ("createdAt", "createdDate", "insertDate", "recordCreated",
                "ingestDate"),
}

# UDL onorbit record -> what this application calls it. One place to correct.
ONORBIT_FIELDS = {
    "sat_no": ("satNo", "satelliteNo", "noradCatId", "noradCatID", "satelliteNumber"),
    "name": ("name", "satName", "altName", "objectName", "commonName",
             "objName", "satelliteName", "origObjectId", "objectId"),
    "int_des": ("intlDes", "internationalDesignator", "intlDesignator"),
    "country": ("countryCode", "country", "origin"),
    "object_type": ("objectType", "type"),
    "launch_date": ("launchDate",),
    "decay_date": ("decayDate",),
}


def _provenance(rec: dict) -> dict:
    """The record's own account of where it came from, as far as it gives one."""
    return {name: str(_first(rec, keys) or "")
            for name, keys in PROVENANCE_FIELDS.items()}


def _first(rec: dict, keys) -> object:
    for key in keys:
        value = rec.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_epoch(raw: str) -> dt.datetime:
    """Parse a UDL timestamp to a naive UTC datetime.

    Lifted from the original. Wrapped so that a malformed upstream timestamp is
    a 502 naming the offending value, not a bare ValueError from inside a loop.
    """
    try:
        text = str(raw)
        text = (text.replace("Z", "+00:00").replace(" ", "T", 1)
                if "T" not in text else text.replace("Z", "+00:00"))
        return dt.datetime.fromisoformat(text).astimezone(dt.UTC).replace(tzinfo=None)
    except (TypeError, ValueError) as exc:
        raise UpstreamError(f"unparseable epoch from the UDL: {raw!r}") from exc


# --------------------------------------------------------------------------- #
#  Element-set reconstruction (lifted unchanged)
# --------------------------------------------------------------------------- #
def _checksum(line: str) -> int:
    total = 0
    for c in line[:68]:
        if c.isdigit():
            total += int(c)
        elif c == "-":
            total += 1
    return total % 10


def elset_to_tle(rec: dict) -> tuple[str, str]:
    """Build TLE lines from a UDL Elset's mean elements when line1/line2 are absent."""
    epoch = parse_epoch(rec["epoch"])
    yr = epoch.year % 100
    doy = (epoch - dt.datetime(epoch.year, 1, 1)).total_seconds() / 86400.0 + 1.0
    satnum = int(rec.get("satNo", 99999))
    try:
        ecc = f"{rec['eccentricity']:.7f}".split(".")[1]
        line1 = (f"1 {satnum:05d}U 00000A   {yr:02d}{doy:012.8f} "
                 f" .00000000  00000-0  00000-0 0  999")
        line2 = (f"2 {satnum:05d} {rec['inclination']:8.4f} {rec['raan']:8.4f} "
                 f"{ecc} {rec['argOfPerigee']:8.4f} {rec['meanAnomaly']:8.4f} "
                 f"{rec['meanMotion']:11.8f}    1")
    except KeyError as exc:
        raise UpstreamError(
            f"UDL elset for satNo {satnum} has neither TLE lines nor the mean "
            f"elements needed to rebuild them (missing {exc.args[0]})") from exc
    return line1 + str(_checksum(line1)), line2 + str(_checksum(line2))


# --------------------------------------------------------------------------- #
#  Client
# --------------------------------------------------------------------------- #
class UDLClient:
    """Thin, rate-limited, HTTP Basic client over the UDL REST surface."""

    def __init__(self, settings, session=None, bucket=None):
        self.settings = settings
        user, password = settings.require_udl()
        self.base = settings.udl_base.rstrip("/")
        self.timeout = settings.udl_timeout_s
        self.bucket = bucket or TokenBucket(settings.udl_rate_per_min)
        if session is not None:
            self.s = session
        else:
            import requests
            self.s = requests.Session()
            self.s.auth = (user, password)
            self.s.headers["Accept"] = "application/json"

    @staticmethod
    def _iso(when: dt.datetime) -> str:
        return when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    def _get(self, path: str, params: dict) -> list:
        """One rate-limited GET returning a decoded JSON list.

        Every failure mode becomes an UpstreamError with a message safe to log:
        the URL and status only, never the credentials or the response body,
        which can echo request content back at us.
        """
        self.bucket.take()
        url = f"{self.base}{path}"
        try:
            r = self.s.get(url, params=params, timeout=self.timeout)
        except Exception as exc:                       # transport, DNS, TLS, timeout
            raise UpstreamError(f"UDL request to {path} failed: {type(exc).__name__}") from exc
        if r.status_code in (401, 403):
            raise UpstreamError(
                f"UDL rejected the credentials on {path} (HTTP {r.status_code}). "
                "Check UDL_USER and UDL_PASS on the app configuration.")
        if r.status_code == 429:
            raise UpstreamError(
                f"UDL rate-limited this app on {path} (HTTP 429). Lower "
                "UDL_RATE_PER_MIN.")
        if r.status_code >= 400:
            raise UpstreamError(f"UDL returned HTTP {r.status_code} for {path}")
        try:
            body = r.json()
        except ValueError as exc:
            raise UpstreamError(f"UDL returned non-JSON for {path}") from exc
        if isinstance(body, dict):
            body = body.get("data") or body.get("results") or []
        if not isinstance(body, list):
            raise UpstreamError(f"UDL returned an unexpected shape for {path}")
        return body

    # --- state vectors ---------------------------------------------------- #
    def state_vectors(self, sat_no, start, end, source="LeoLabs",
                      data_mode="REAL", default_frame="J2000") -> list:
        params = {
            "epoch": f"{self._iso(start)}..{self._iso(end)}",
            "satNo": sat_no,
            "source": source,
            "maxResults": self.settings.max_results,
        }
        if data_mode:
            params["dataMode"] = data_mode
        out, missing = [], 0
        for rec in self._get("/udl/statevector", params):
            frame = rec.get("referenceFrame") or default_frame
            if not rec.get("referenceFrame"):
                missing += 1
            out.append(StateVector(
                epoch=parse_epoch(rec["epoch"]),
                r=np.array([rec["xpos"], rec["ypos"], rec["zpos"]], dtype=float),
                v=np.array([rec["xvel"], rec["yvel"], rec["zvel"]], dtype=float),
                frame=frame,
                **_provenance(rec),
            ))
        if missing:
            # The original printed this to stderr. It matters: a record with no
            # referenceFrame is assumed to be in the provider's declared frame,
            # and if that assumption is wrong the offsets are wrong.
            event("udl.statevector.frame_assumed", source=source, sat_no=sat_no,
                  assumed_frame=default_frame, records_without_frame=missing,
                  records_total=len(out))
        return out

    # --- element sets ----------------------------------------------------- #
    def elsets(self, sat_no, start, end, data_mode="REAL") -> list:
        params = {
            "epoch": f"{self._iso(start)}..{self._iso(end)}",
            "satNo": sat_no,
            "maxResults": self.settings.max_results,
        }
        if data_mode:
            params["dataMode"] = data_mode
        out = []
        for rec in self._get("/udl/elset", params):
            line1, line2 = rec.get("line1"), rec.get("line2")
            if not (line1 and line2):
                line1, line2 = elset_to_tle(rec)   # rebuild from mean elements
            out.append(Elset(epoch=parse_epoch(rec["epoch"]), line1=line1,
                             line2=line2, **_provenance(rec)))
        if out:
            # This query is deliberately not filtered by source, so what comes
            # back is whatever the tenant holds. Which originators those are is
            # a question about the tenant that no amount of reading the data
            # model answers, so the answer is logged the first time a render
            # asks for it.
            sources = sorted({rec.source or UNATTRIBUTED for rec in out})
            event("udl.elset.sources", sat_no=sat_no, sources=sources,
                  records=len(out))
        return out

    # --- catalogue, for the picker ---------------------------------------- #
    def _onorbit(self, params: dict) -> list:
        records = self._get("/udl/onorbit", {"maxResults": 500, **params})
        out = []
        nameless = None
        for rec in records:
            sat_no = _first(rec, ONORBIT_FIELDS["sat_no"])
            if sat_no is None:
                continue
            try:
                sat_no = int(sat_no)
            except (TypeError, ValueError):
                continue
            name = _first(rec, ONORBIT_FIELDS["name"])
            if name is None and nameless is None:
                nameless = sorted(rec.keys())
            out.append({
                "satNo": sat_no,
                "name": str(name or f"OBJECT {sat_no}"),
                "intlDes": _first(rec, ONORBIT_FIELDS["int_des"]),
                "country": _first(rec, ONORBIT_FIELDS["country"]),
                "objectType": _first(rec, ONORBIT_FIELDS["object_type"]),
                "launchDate": _first(rec, ONORBIT_FIELDS["launch_date"]),
                "decayDate": _first(rec, ONORBIT_FIELDS["decay_date"])})
        if nameless is not None:
            # The picker fell back to "OBJECT <number>" because none of the
            # aliases in ONORBIT_FIELDS["name"] matched this tenant's records.
            # The record's field names are logged so the right one can be added
            # in a single edit rather than guessed at. Keys only, never values.
            event("udl.onorbit.name_missing",
                  tried=",".join(ONORBIT_FIELDS["name"]),
                  record_fields=",".join(nameless))
        return out

    def objects_by_satno(self, sat_nos) -> dict:
        """{satNo: record} for the given NORAD numbers. One request."""
        wanted = sorted({int(s) for s in sat_nos})
        if not wanted:
            return {}
        found = self._onorbit({"satNo": ",".join(str(s) for s in wanted)})
        return {rec["satNo"]: rec for rec in found if rec["satNo"] in set(wanted)}

    # --- source availability -------------------------------------------- #
    def probe_source(self, source: dict, sat_no: int, start, end,
                     data_mode="REAL") -> dict:
        """Ask the UDL for a single record from one provider.

        The udl_source strings in STATE_SOURCES are the names these providers
        are known by, not values read back from a tenant, and a tenant that
        spells one differently answers with an empty list rather than an error.
        That failure is invisible in a report: the provider just never appears.
        This turns it into something you can see.
        """
        params = {
            "epoch": f"{self._iso(start)}..{self._iso(end)}",
            "satNo": sat_no,
            "source": source["udl_source"],
            "maxResults": 1,
        }
        if data_mode:
            params["dataMode"] = DATA_MODES.get(data_mode, data_mode)
        row = {"key": source["key"], "label": source["label"], "udlSource": source["udl_source"]}
        try:
            records = self._get("/udl/statevector", params)
        except UpstreamError as exc:
            return {**row, "available": False, "records": 0, "error": str(exc)}
        return {**row, "available": bool(records), "records": len(records),
                "error": None}

    def probe_sources(self, sat_no: int, start, end, sources=None,
                      data_mode="REAL") -> list:
        """One record requested per provider. Five cheap, rate-limited calls."""
        chosen = sources if sources is not None else STATE_SOURCES
        results = [self.probe_source(src, sat_no, start, end, data_mode)
                   for src in chosen]
        event("sources.probed", sat_no=sat_no,
              available=",".join(r["key"] for r in results if r["available"]) or "none",
              missing=",".join(r["key"] for r in results if not r["available"]) or "none")
        return results

    def search_objects(self, query: str, limit: int = 50) -> list:
        """Catalogue search for the picker.

        A numeric query is a NORAD lookup. Anything else is a name search: the
        UDL name filter is tried first, and if the tenant does not support the
        parameter the result is filtered locally over a bounded page rather
        than returning nothing.
        """
        text = (query or "").strip()
        if not text:
            return []
        if text.isdigit():
            return list(self.objects_by_satno([int(text)]).values())[:limit]
        upper = text.upper()
        records = self._onorbit({"name": f"~{upper}", "maxResults": max(limit * 4, 100)})
        matched = [r for r in records if upper in (r["name"] or "").upper()]
        return (matched or records)[:limit]
