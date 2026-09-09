"""Satellite groups: the store behind the picker.

Groups used to be ``[group:Name]`` sections in a local ini file that the
operator hand-edited. They are now created in the application from UDL
catalogue selections, which changes three things:

  * Where they live. A JSON document on the App Store persistent volume
    (STORAGE_MOUNT_PATH, default /data), written atomically: to a temporary
    file in the same directory, then renamed over the target. A crash or a
    killed pod mid-write therefore leaves the previous document intact rather
    than a truncated one. Note the volume needs `fsGroup` set in the pod
    securityContext; without it a root-owned mount returns EACCES on every
    write while the app otherwise looks healthy.

  * Who can change them. Several people share one deployment, so a blind write
    would let the last save silently discard someone else's edit. The document
    carries a monotonic revision; a write must present the revision it read or
    it is refused with a conflict. Callers can then re-read and retry.

  * Whether the contents can be trusted. They cannot. Everything arriving from
    a caller is validated here, at the boundary, and rejected rather than
    coerced. Names are length-bounded and stripped of control characters
    because they are reflected into HTML and into log fields.

Deletes archive rather than erase. A mis-click in a picker should not be the
end of a group definition somebody spent time assembling.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path

from .audit import event, safe
from .errors import ConflictError, NotFoundError, ValidationError

MAX_NAME = 80
MAX_SATS = 40
MIN_SATS = 2
# NORAD catalogue numbers. Five digits historically, nine in the extended
# catalogue; anything outside that is not a satellite number.
MIN_SAT_NO = 1
MAX_SAT_NO = 999_999_999
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The groups that lived in the committed credentials.ini, kept so the move to
# the new store loses nothing. Seeded once, on first boot with an empty store.
SEED_GROUPS = [
    dict(name="SPIDER BABIES w/ICEYE",
         sats=[68764, 68763, 68762, 68759, 68754, 59102, 59103], reference=68762),
    dict(name="COSMOS 2581/82/83", sats=[62902, 62903, 62904], reference=62902),
    dict(name="PRC SpacePlane 4", sats=[67689, 69673, 59884, 99995], reference=67689),
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
#  Validation
# --------------------------------------------------------------------------- #
def clean_name(raw) -> str:
    """Validate a group name. Rejected, never silently trimmed into something
    else, except for control characters and surrounding whitespace."""
    if not isinstance(raw, str):
        raise ValidationError("group name must be a string")
    name = _CONTROL.sub("", raw).strip()
    if not name:
        raise ValidationError("group name cannot be empty")
    if len(name) > MAX_NAME:
        raise ValidationError(f"group name cannot exceed {MAX_NAME} characters")
    return name


def clean_sat_no(raw) -> int:
    """Validate one NORAD number."""
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValidationError(f"not a NORAD number: {safe(raw)!r}")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValidationError(f"not a NORAD number: {safe(raw)!r}") from None
    if not MIN_SAT_NO <= value <= MAX_SAT_NO:
        raise ValidationError(
            f"NORAD number out of range: {value} (expected "
            f"{MIN_SAT_NO} to {MAX_SAT_NO})")
    return value


def clean_sats(raw) -> list:
    """Validate the member list: order preserved, duplicates removed."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValidationError("sats must be a list of NORAD numbers")
    seen, out = set(), []
    for item in raw:
        value = clean_sat_no(item)
        if value not in seen:
            seen.add(value)
            out.append(value)
    if len(out) < MIN_SATS:
        raise ValidationError(
            f"a group needs at least {MIN_SATS} distinct objects to show a "
            f"relative offset, got {len(out)}")
    if len(out) > MAX_SATS:
        raise ValidationError(f"a group cannot exceed {MAX_SATS} objects")
    return out


def clean_group(payload: dict, group_id: str | None = None) -> dict:
    """Validate a whole submitted group. The reference must be a member."""
    if not isinstance(payload, dict):
        raise ValidationError("group must be an object")
    name = clean_name(payload.get("name"))
    sats = clean_sats(payload.get("sats"))
    raw_ref = payload.get("reference")
    reference = sats[0] if raw_ref in (None, "") else clean_sat_no(raw_ref)
    if reference not in sats:
        raise ValidationError(
            f"the reference object {reference} is not a member of the group; "
            "the waterfall is anchored on one of its own objects")
    return dict(id=group_id or str(uuid.uuid4()), name=name, sats=sats,
                reference=reference, archived=False,
                created=_now(), updated=_now())


# --------------------------------------------------------------------------- #
#  Store
# --------------------------------------------------------------------------- #
class GroupStore:
    """The group document on the persistent volume.

    One process, one file, one lock. The lock serialises writes within the pod;
    the revision check catches the cross-request case where two people read the
    same state and both save. Both are needed: the lock alone would let the
    second write win silently.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # --- document level ---------------------------------------------------- #
    def _empty(self) -> dict:
        return dict(rev=0, groups=[])

    def _read(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            # A corrupt store is not something to paper over by starting empty:
            # that would look like "all your groups vanished" and then help by
            # overwriting the evidence.
            raise ValidationError(
                f"the group store at {self.path} is unreadable ({type(exc).__name__}). "
                "It has not been modified. Move it aside to start fresh."
            ) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("groups"), list):
            raise ValidationError(f"the group store at {self.path} is not a group document")
        doc.setdefault("rev", 0)
        return doc

    def _write(self, doc: dict) -> dict:
        """Atomic replace: temp file in the same directory, fsync, rename."""
        doc["rev"] = int(doc.get("rev", 0)) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = json.dumps(doc, indent=2, sort_keys=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ValidationError(
                f"could not write the group store at {self.path}: {type(exc).__name__}. "
                "If this is EACCES, the storage volume needs fsGroup set in the "
                "pod securityContext so a non-root container can write to it."
            ) from exc
        return doc

    def _check_rev(self, doc: dict, expected) -> None:
        if expected is None:
            return
        if int(expected) != int(doc["rev"]):
            raise ConflictError(
                f"the group list changed since you loaded it (you have revision "
                f"{expected}, the store is at {doc['rev']}). Reload and reapply "
                "your change so you do not overwrite someone else's edit.")

    # --- reads ------------------------------------------------------------- #
    def load(self, include_archived: bool = False) -> dict:
        with self._lock:
            doc = self._read()
        groups = [g for g in doc["groups"] if include_archived or not g.get("archived")]
        return dict(rev=doc["rev"], groups=groups)

    def get(self, group_id: str) -> dict:
        for group in self.load(include_archived=True)["groups"]:
            if group["id"] == group_id:
                return group
        raise NotFoundError(f"no group with id {safe(group_id)}")

    def active(self) -> list:
        """Groups to render, in stored order."""
        return self.load()["groups"]

    # --- writes ------------------------------------------------------------ #
    def create(self, payload: dict) -> dict:
        group = clean_group(payload)
        with self._lock:
            doc = self._read()
            self._reject_duplicate_name(doc, group["name"], None)
            doc["groups"].append(group)
            self._write(doc)
        event("group.created", group_id=group["id"], group=group["name"],
              sats=len(group["sats"]), reference=group["reference"], rev=doc["rev"])
        return group

    def update(self, group_id: str, payload: dict, expected_rev=None) -> dict:
        with self._lock:
            doc = self._read()
            self._check_rev(doc, expected_rev)
            index = self._index_of(doc, group_id)
            existing = doc["groups"][index]
            group = clean_group(payload, group_id=group_id)
            group["created"] = existing.get("created", group["created"])
            group["archived"] = bool(existing.get("archived", False))
            self._reject_duplicate_name(doc, group["name"], group_id)
            doc["groups"][index] = group
            self._write(doc)
        event("group.updated", group_id=group_id, group=group["name"],
              sats=len(group["sats"]), reference=group["reference"], rev=doc["rev"])
        return group

    def archive(self, group_id: str, expected_rev=None) -> dict:
        """Archive rather than erase. Restorable, and the definition survives."""
        with self._lock:
            doc = self._read()
            self._check_rev(doc, expected_rev)
            index = self._index_of(doc, group_id)
            group = doc["groups"][index]
            group["archived"] = True
            group["updated"] = _now()
            self._write(doc)
        event("group.archived", group_id=group_id, group=group["name"], rev=doc["rev"])
        return group

    def restore(self, group_id: str, expected_rev=None) -> dict:
        with self._lock:
            doc = self._read()
            self._check_rev(doc, expected_rev)
            index = self._index_of(doc, group_id)
            group = doc["groups"][index]
            self._reject_duplicate_name(doc, group["name"], group_id)
            group["archived"] = False
            group["updated"] = _now()
            self._write(doc)
        event("group.restored", group_id=group_id, group=group["name"], rev=doc["rev"])
        return group

    def seed_if_empty(self, groups=None) -> int:
        """Populate an empty store once, so the move from credentials.ini does
        not lose the groups that were in it. Never overwrites existing data."""
        with self._lock:
            doc = self._read()
            if doc["groups"]:
                return 0
            for payload in (groups if groups is not None else SEED_GROUPS):
                doc["groups"].append(clean_group(payload))
            count = len(doc["groups"])
            self._write(doc)
        event("group.seeded", count=count, rev=doc["rev"])
        return count

    # --- helpers ----------------------------------------------------------- #
    @staticmethod
    def _index_of(doc: dict, group_id: str) -> int:
        for i, group in enumerate(doc["groups"]):
            if group["id"] == group_id:
                return i
        raise NotFoundError(f"no group with id {safe(group_id)}")

    @staticmethod
    def _reject_duplicate_name(doc: dict, name: str, group_id) -> None:
        """Names are the tab labels, so two live groups sharing one is a
        usability trap rather than a data problem. Archived names are free."""
        lowered = name.casefold()
        for group in doc["groups"]:
            if group["id"] == group_id or group.get("archived"):
                continue
            if group["name"].casefold() == lowered:
                raise ValidationError(f"a group named {name!r} already exists")
