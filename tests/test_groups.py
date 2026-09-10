"""The group store: validation at the boundary, atomic writes, and the
revision check that stops one person's save discarding another's."""

from __future__ import annotations

import builtins
import errno
import json
import os
import pathlib
import threading

import pytest

from timeslides.errors import ConflictError, NotFoundError, ValidationError
from timeslides.groups import (MAX_NAME, MAX_SATS, MIN_SATS, SEED_GROUPS, GroupStore,
                               clean_group, clean_name, clean_sat_no, clean_sats)


@pytest.fixture
def store(tmp_path):
    return GroupStore(tmp_path / "groups.json")


def _refuse_replace(*_args, **_kwargs):
    """Injected in place of os.replace, so the atomic rename fails."""
    raise OSError("no")


def _refusing(code):
    """A stand-in for a syscall the filesystem does not implement."""

    def refuse(*_args, **_kwargs):
        raise OSError(code, os.strerror(code))

    return refuse


def _block_all_writes(monkeypatch, code=errno.EROFS):
    """Make every write mechanism in the ladder fail, the way a volume that is
    genuinely unusable does."""
    monkeypatch.setattr(os, "replace", _refusing(code))
    monkeypatch.setattr(builtins, "open", _refusing(code))


def _group(**over):
    base = dict(name="PRC SpacePlane 4", sats=[67689, 69673, 59884], reference=67689)
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
#  Name validation
# --------------------------------------------------------------------------- #
def test_a_normal_name_is_kept_as_written():
    assert clean_name("SPIDER BABIES w/ICEYE") == "SPIDER BABIES w/ICEYE"


def test_surrounding_whitespace_is_trimmed():
    assert clean_name("  COSMOS 2581  ") == "COSMOS 2581"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t", None, 5, [], {}])
def test_an_empty_or_non_string_name_is_rejected(bad):
    with pytest.raises(ValidationError):
        clean_name(bad)


def test_control_characters_are_stripped_from_names():
    """Names reach HTML and log fields. A newline in one could forge a log line."""
    assert clean_name("A\nB\x00C") == "ABC"


def test_an_over_long_name_is_rejected_not_truncated():
    with pytest.raises(ValidationError, match=f"exceed {MAX_NAME}"):
        clean_name("x" * (MAX_NAME + 1))
    assert len(clean_name("x" * MAX_NAME)) == MAX_NAME


def test_a_name_that_looks_like_markup_is_stored_verbatim_for_later_escaping():
    """The store is not the escaping layer; the renderer is. Mangling the name
    here would corrupt legitimate names containing punctuation."""
    hostile = '</script><img src=x onerror=alert(1)>'
    assert clean_name(hostile) == hostile


# --------------------------------------------------------------------------- #
#  NORAD number validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [(59884, 59884), ("59884", 59884),
                                      ("  68762 ", 68762), (1, 1)])
def test_norad_numbers_are_accepted_as_int_or_digit_string(raw, want):
    assert clean_sat_no(raw) == want


@pytest.mark.parametrize("bad", ["abc", "", None, 1.5, [], "12a", True, False,
                                 "0x10", "1e3"])
def test_a_non_numeric_norad_value_is_rejected(bad):
    with pytest.raises(ValidationError, match="not a NORAD number"):
        clean_sat_no(bad)


@pytest.mark.parametrize("bad", [0, -1, 1_000_000_000])
def test_an_out_of_range_norad_number_is_rejected(bad):
    with pytest.raises(ValidationError, match="out of range"):
        clean_sat_no(bad)


# --------------------------------------------------------------------------- #
#  Member list validation
# --------------------------------------------------------------------------- #
def test_member_order_is_preserved_and_duplicates_removed():
    assert clean_sats([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_a_group_needs_at_least_two_distinct_objects():
    """One object against itself is a flat line at zero, which is not a
    phase-offset plot."""
    with pytest.raises(ValidationError, match=f"at least {MIN_SATS}"):
        clean_sats([59884])
    with pytest.raises(ValidationError, match=f"at least {MIN_SATS}"):
        clean_sats([59884, 59884])


def test_an_over_large_group_is_rejected():
    too_many = list(range(1, MAX_SATS + 2))
    with pytest.raises(ValidationError, match=f"exceed {MAX_SATS}"):
        clean_sats(too_many)


@pytest.mark.parametrize("bad", ["59884 67689", b"x", 5, None, {"a": 1}])
def test_sats_must_be_a_list_not_a_string_or_scalar(bad):
    with pytest.raises(ValidationError, match="must be a list"):
        clean_sats(bad)


# --------------------------------------------------------------------------- #
#  Whole-group validation
# --------------------------------------------------------------------------- #
def test_a_valid_group_gets_an_id_and_timestamps():
    g = clean_group(_group())
    assert g["id"] and g["created"] and g["updated"]
    assert g["archived"] is False


def test_the_reference_defaults_to_the_first_member():
    for missing in (None, ""):
        g = clean_group(_group(reference=missing))
        assert g["reference"] == 67689


def test_a_reference_outside_the_group_is_rejected():
    """The waterfall is anchored on one of the group's own objects."""
    payload = _group(reference=11111)
    with pytest.raises(ValidationError, match="not a member of the group"):
        clean_group(payload)


def test_a_non_object_payload_is_rejected():
    with pytest.raises(ValidationError, match="must be an object"):
        clean_group(["not", "a", "dict"])


def test_unknown_keys_in_the_payload_are_ignored_not_stored():
    """A caller cannot smuggle fields into the document, including an id or an
    archived flag it should not control."""
    g = clean_group(_group(id="chosen-by-caller", archived=True, evil="x"))
    assert g["id"] != "chosen-by-caller"
    assert g["archived"] is False
    assert "evil" not in g


# --------------------------------------------------------------------------- #
#  Create, read, update
# --------------------------------------------------------------------------- #
def test_an_absent_store_reads_as_empty_at_revision_zero(store):
    assert store.load() == dict(rev=0, groups=[])


def test_a_created_group_is_readable_and_bumps_the_revision(store):
    created = store.create(_group())
    doc = store.load()
    assert doc["rev"] == 1
    assert [g["id"] for g in doc["groups"]] == [created["id"]]
    assert store.get(created["id"])["name"] == "PRC SpacePlane 4"


def test_getting_an_unknown_id_is_a_not_found(store):
    with pytest.raises(NotFoundError, match="no group with id"):
        store.get("nope")


def test_two_live_groups_cannot_share_a_name(store):
    store.create(_group())
    same_name = _group(sats=[1, 2, 3], reference=1)
    with pytest.raises(ValidationError, match="already exists"):
        store.create(same_name)


def test_the_duplicate_name_check_is_case_insensitive(store):
    store.create(_group(name="Cosmos"))
    shouting = _group(name="COSMOS", sats=[1, 2], reference=1)
    with pytest.raises(ValidationError, match="already exists"):
        store.create(shouting)


def test_an_update_replaces_the_members_and_keeps_the_id_and_created_time(store):
    created = store.create(_group())
    updated = store.update(created["id"], _group(sats=[1, 2, 3, 4], reference=2))
    assert updated["id"] == created["id"]
    assert updated["created"] == created["created"]
    assert updated["sats"] == [1, 2, 3, 4]
    assert updated["reference"] == 2


def test_a_group_can_be_renamed_to_its_own_name(store):
    """Renaming to the same name must not trip the duplicate check."""
    created = store.create(_group())
    assert store.update(created["id"], _group())["name"] == created["name"]


def test_updating_an_unknown_id_is_a_not_found(store):
    payload = _group()
    with pytest.raises(NotFoundError):
        store.update("nope", payload)


def test_an_invalid_update_leaves_the_stored_group_untouched(store):
    created = store.create(_group())
    too_few = _group(sats=[59884])
    with pytest.raises(ValidationError):
        store.update(created["id"], too_few)
    assert store.get(created["id"])["sats"] == created["sats"]
    assert store.load()["rev"] == 1              # no revision burned on a reject


# --------------------------------------------------------------------------- #
#  Archive and restore. A mis-click should not destroy a definition.
# --------------------------------------------------------------------------- #
def test_archiving_hides_a_group_but_keeps_it(store):
    created = store.create(_group())
    store.archive(created["id"])
    assert store.load()["groups"] == []
    assert store.active() == []
    kept = store.get(created["id"])
    assert kept["archived"] is True
    assert kept["sats"] == created["sats"]


def test_an_archived_group_can_be_restored(store):
    created = store.create(_group())
    store.archive(created["id"])
    store.restore(created["id"])
    assert [g["id"] for g in store.active()] == [created["id"]]


def test_an_archived_name_is_free_for_reuse(store):
    created = store.create(_group())
    store.archive(created["id"])
    store.create(_group(sats=[1, 2], reference=1))          # same name, allowed
    assert len(store.active()) == 1


def test_restoring_a_group_whose_name_was_reused_is_refused(store):
    """Otherwise the restore would create two live tabs with one label."""
    created = store.create(_group())
    store.archive(created["id"])
    store.create(_group(sats=[1, 2], reference=1))
    with pytest.raises(ValidationError, match="already exists"):
        store.restore(created["id"])


def test_archiving_an_unknown_id_is_a_not_found(store):
    with pytest.raises(NotFoundError):
        store.archive("nope")


# --------------------------------------------------------------------------- #
#  Concurrency
# --------------------------------------------------------------------------- #
def test_a_write_carrying_a_stale_revision_is_refused(store):
    created = store.create(_group())
    stale = store.load()["rev"]
    store.update(created["id"], _group(sats=[1, 2], reference=1))     # someone else
    mine = _group(sats=[5, 6], reference=5)
    with pytest.raises(ConflictError, match="changed since you loaded it"):
        store.update(created["id"], mine, expected_rev=stale)
    assert store.get(created["id"])["sats"] == [1, 2]                 # not clobbered


def test_a_write_carrying_the_current_revision_succeeds(store):
    created = store.create(_group())
    rev = store.load()["rev"]
    store.update(created["id"], _group(sats=[9, 8], reference=9), expected_rev=rev)
    assert store.get(created["id"])["sats"] == [9, 8]


def test_omitting_the_revision_is_a_deliberate_unconditional_write(store):
    created = store.create(_group())
    store.update(created["id"], _group(sats=[1, 2], reference=1))
    store.update(created["id"], _group(sats=[3, 4], reference=3))
    assert store.get(created["id"])["sats"] == [3, 4]


def test_archive_also_honours_the_revision_check(store):
    created = store.create(_group())
    stale = store.load()["rev"]
    store.update(created["id"], _group(sats=[1, 2], reference=1))
    with pytest.raises(ConflictError):
        store.archive(created["id"], expected_rev=stale)


def test_concurrent_creates_all_land_and_the_revision_counts_them(store):
    """The in-pod lock has to serialise writes or the file loses entries."""
    errors = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        try:
            store.create(_group(name=f"GROUP {i}", sats=[i + 1, i + 2], reference=i + 1))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    doc = store.load()
    assert len(doc["groups"]) == 8
    assert doc["rev"] == 8


# --------------------------------------------------------------------------- #
#  Durability
# --------------------------------------------------------------------------- #
def test_the_write_is_atomic_and_leaves_no_temporary_file(store, tmp_path):
    store.create(_group())
    assert store.path.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name != "groups.json"] == []


def test_the_document_on_disk_is_readable_json_with_a_revision(store):
    store.create(_group())
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["rev"] == 1
    assert doc["groups"][0]["name"] == "PRC SpacePlane 4"


def test_a_corrupt_store_is_reported_and_not_overwritten(tmp_path):
    """Starting empty would read as 'all your groups vanished' and then destroy
    the evidence. Refuse instead."""
    path = tmp_path / "groups.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = GroupStore(path)
    with pytest.raises(ValidationError, match="unreadable"):
        store.load()
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_a_json_document_of_the_wrong_shape_is_rejected(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text('{"groups": "not a list"}', encoding="utf-8")
    store = GroupStore(path)
    with pytest.raises(ValidationError, match="not a group document"):
        store.load()


def test_a_write_failure_explains_the_fsgroup_trap(tmp_path, monkeypatch):
    """The classic degraded pod: healthy app, root-owned volume, EACCES on
    every save. The reported reason needs to name the cause.

    The failure is injected rather than produced with chmod, because the test
    suite may run as root (it does in the build container) and root ignores
    directory permissions, which would make this test silently pass for the
    wrong reason.
    """
    store = GroupStore(tmp_path / "groups.json")
    _block_all_writes(monkeypatch, errno.EACCES)
    store.create(_group())
    assert "fsGroup" in store.fallback_reason


def test_a_rename_failure_falls_through_to_writing_in_place(tmp_path, monkeypatch):
    """An S3-backed mount is what the app is actually given, and those
    typically implement neither fsync nor rename.

    Refusing the save was the old behaviour and it made the deployed
    application useless. The store drops to the next mechanism instead, and the
    group is both saved and on the volume.
    """
    store = GroupStore(tmp_path / "groups.json")
    monkeypatch.setattr(os, "replace", _refusing(errno.ENOSYS))
    monkeypatch.setattr(os, "fsync", _refusing(errno.ENOSYS))
    made = store.create(_group())
    assert store.strategy == "direct"
    assert store.persistent, "this must stay on the volume, not go to memory"
    assert [g["id"] for g in store.active()] == [made["id"]]
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert [g["id"] for g in on_disk["groups"]] == [made["id"]]


def test_a_mount_that_refuses_to_overwrite_is_rewritten_from_scratch(tmp_path,
                                                                     monkeypatch):
    """Some object-store mounts take a new key written sequentially but refuse
    to open an existing one for writing."""
    store = GroupStore(tmp_path / "groups.json")
    store.path.write_text('{"rev": 0, "groups": []}', encoding="utf-8")
    monkeypatch.setattr(os, "replace", _refusing(errno.ENOSYS))
    real_open = builtins.open

    def refuse_existing(file, mode="r", *args, **kwargs):
        if "w" in mode and pathlib.Path(file).exists():
            raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse_existing)
    made = store.create(_group())
    assert store.strategy == "recreate"
    assert store.persistent
    monkeypatch.undo()
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert [g["id"] for g in on_disk["groups"]] == [made["id"]]


def test_recreate_does_not_remove_the_target_until_a_write_has_succeeded(
        tmp_path, monkeypatch):
    """The rung that replaces the file must not be the reason the file is gone.

    Asserted directly against the strategy, because through the ladder the
    earlier rungs mask it: this is the guarantee the rung itself has to make.
    """
    from timeslides.storage import _write_recreate

    target = tmp_path / "groups.json"
    target.write_text('{"rev": 1, "groups": []}', encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    monkeypatch.setattr(builtins, "open", _refusing(errno.EROFS))
    with pytest.raises(OSError):
        _write_recreate(target, '{"rev": 2, "groups": []}')
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == before


def test_recreate_leaves_nothing_beside_the_target(tmp_path):
    from timeslides.storage import _write_recreate

    target = tmp_path / "groups.json"
    _write_recreate(target, '{"rev": 1, "groups": []}')
    assert [f.name for f in tmp_path.iterdir()] == ["groups.json"]
    _write_recreate(target, '{"rev": 2, "groups": []}')
    assert [f.name for f in tmp_path.iterdir()] == ["groups.json"]
    assert json.loads(target.read_text(encoding="utf-8"))["rev"] == 2


def test_an_unimplemented_fsync_does_not_fail_a_save(tmp_path, monkeypatch):
    """fsync is durability, not atomicity. The temp-and-rename is what makes
    the replacement atomic, so a mount without fsync keeps the good strategy."""
    store = GroupStore(tmp_path / "groups.json")
    monkeypatch.setattr(os, "fsync", _refusing(errno.ENOSYS))
    made = store.create(_group())
    assert store.strategy == "atomic"
    assert [g["id"] for g in store.active()] == [made["id"]]


def test_a_real_fsync_error_is_not_swallowed(tmp_path, monkeypatch):
    """ENOSYS means the call does not exist. EIO means the write itself failed,
    and that must not be waved through as a filesystem quirk.

    Only fsync is broken here, so every rung of the ladder reaches it and every
    rung fails. The store degrades to memory rather than reporting a save that
    never landed.
    """
    store = GroupStore(tmp_path / "groups.json")
    monkeypatch.setattr(os, "fsync", _refusing(errno.EIO))
    store.create(_group())
    assert not store.persistent
    assert "EIO" in store.fallback_reason


def test_a_parent_that_cannot_be_created_is_reported(tmp_path, monkeypatch):
    """No directory, and nothing can be written into it either."""
    store = GroupStore(tmp_path / "nested" / "groups.json")
    monkeypatch.setattr(pathlib.Path, "mkdir", _refusing(errno.EROFS))
    monkeypatch.setattr(builtins, "open", _refusing(errno.EROFS))
    ok, detail = store.writable()
    assert ok is False
    assert "EROFS" in detail


def test_a_refused_mkdir_does_not_by_itself_fail_a_write(tmp_path, monkeypatch):
    """On an object-store mount there are no real directories: the separator
    only looks like one. mkdir can fail with ENOSYS on a path that is
    perfectly writable, and mountpoint-for-s3 refuses mkdir while accepting a
    write to a key beneath it. Raising on mkdir would turn a working volume
    into an unusable one, so the write is the arbiter."""
    store = GroupStore(tmp_path / "groups.json")
    calls = []

    def refuse(self, *_a, **_k):
        calls.append(self)
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    # is_dir false everywhere forces the mkdir attempt even though tmp_path
    # exists, which is exactly the object-store shape.
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(pathlib.Path, "mkdir", refuse)
    ok, detail = store.writable()
    assert calls, "mkdir should have been attempted"
    assert ok is True, detail
    made = store.create(_group())
    assert [g["id"] for g in store.active()] == [made["id"]]
    assert store.persistent


def test_a_corrupt_document_does_not_stop_the_fallback(tmp_path):
    """Falling back seeds from the volume where it can be read. An unreadable
    file must not turn the fallback itself into an error."""
    path = tmp_path / "groups.json"
    path.write_text("{ not json", encoding="utf-8")
    store = GroupStore(path)
    store.use_memory_fallback("test")
    assert store.load() == {"rev": 0, "groups": []}
    made = store.create(_group())
    assert [g["id"] for g in store.active()] == [made["id"]]
    assert path.read_text(encoding="utf-8") == "{ not json", \
        "the unreadable file is evidence; it must not be overwritten"


def test_a_failed_write_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    store = GroupStore(tmp_path / "groups.json")
    monkeypatch.setattr(os, "replace", _refuse_replace)
    store.create(_group())
    monkeypatch.undo()
    leftovers = [f.name for f in tmp_path.iterdir() if ".tmp" in f.name]
    assert leftovers == []


def test_a_volume_that_stops_working_does_not_damage_what_is_on_it(tmp_path,
                                                                   monkeypatch):
    """A save that cannot reach the volume must leave the volume as it was, and
    must still keep the group for the life of the pod.

    This is the regression test for a real data-loss bug in the write ladder.
    The recreate strategy removes the target before rewriting it, and its first
    version did so with no way back, so on a volume where the write then also
    failed the existing document was simply gone. Nothing is removed now until
    the mount has proved it will take a write.
    """
    store = GroupStore(tmp_path / "groups.json")
    first = store.create(_group())
    before = store.path.read_text(encoding="utf-8")
    _block_all_writes(monkeypatch)
    second = store.create(_group(name="SECOND", sats=[1, 2], reference=1))
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == before
    assert not store.persistent
    # Both groups are still there: the one on the volume and the one that only
    # ever made it to memory.
    assert [g["id"] for g in store.active()] == [first["id"], second["id"]]


def test_the_store_creates_its_parent_directory(tmp_path):
    store = GroupStore(tmp_path / "nested" / "deeper" / "groups.json")
    store.create(_group())
    assert store.path.exists()


# --------------------------------------------------------------------------- #
#  Seeding: the migration from credentials.ini
# --------------------------------------------------------------------------- #
def test_seeding_an_empty_store_loads_the_groups_from_the_old_ini_file(store):
    assert store.seed_if_empty() == len(SEED_GROUPS)
    names = [g["name"] for g in store.active()]
    assert names == [g["name"] for g in SEED_GROUPS]


def test_the_seeded_groups_keep_their_references(store):
    store.seed_if_empty()
    by_name = {g["name"]: g for g in store.active()}
    assert by_name["SPIDER BABIES w/ICEYE"]["reference"] == 68762
    assert by_name["COSMOS 2581/82/83"]["reference"] == 62902
    assert by_name["PRC SpacePlane 4"]["reference"] == 67689


def test_seeding_never_touches_a_populated_store(store):
    created = store.create(_group(name="MINE"))
    assert store.seed_if_empty() == 0
    assert [g["id"] for g in store.active()] == [created["id"]]


def test_seeding_is_idempotent(store):
    assert store.seed_if_empty() == len(SEED_GROUPS)
    assert store.seed_if_empty() == 0
    assert len(store.active()) == len(SEED_GROUPS)


def test_every_seed_group_passes_the_same_validation_as_a_submitted_one():
    for payload in SEED_GROUPS:
        clean_group(payload)


# --------------------------------------------------------------------------- #
#  Write failures must name the real cause
#
#  The first version of the message said "if this is EACCES, set fsGroup" for
#  every failure. A live deployment then reported a bare OSError, which cannot
#  be EACCES: Python raises PermissionError for EACCES and EPERM, so a plain
#  OSError is something else and the message sent the operator after the wrong
#  cause. These tests hold each errno to its own advice.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code,expect", [
    (errno.EACCES, "fsGroup"),
    (errno.EPERM, "fsGroup"),
    (errno.EROFS, "no storage volume is mounted"),
    (errno.ENOSPC, "volume is full"),
    (errno.EDQUOT, "quota is exhausted"),
    (errno.EXDEV, "different filesystems"),
    (errno.ENOENT, "does not exist"),
])
def test_each_write_failure_gets_advice_matched_to_its_errno(code, expect):
    from timeslides.groups import write_failure_advice
    advice = write_failure_advice(OSError(code, os.strerror(code)))
    assert expect in advice
    assert errno.errorcode[code] in advice, "the errno name must be quoted"
    assert f"errno {code}" in advice


def test_a_read_only_volume_is_not_reported_as_a_permissions_problem():
    """The exact confusion from the live deployment."""
    from timeslides.groups import write_failure_advice
    advice = write_failure_advice(OSError(errno.EROFS, os.strerror(errno.EROFS)))
    assert "fsGroup" not in advice
    assert "read-only" in advice
    assert "storage add-on" in advice


def test_an_unrecognised_errno_still_reports_what_the_os_said():
    from timeslides.groups import write_failure_advice
    advice = write_failure_advice(OSError(9999, "something odd"))
    assert "something odd" in advice
    assert "errno 9999" in advice


def test_the_store_reports_the_real_errno_when_every_write_fails(tmp_path,
                                                                 monkeypatch):
    store = GroupStore(tmp_path / "groups.json")
    _block_all_writes(monkeypatch, errno.EROFS)
    store.create(_group())
    assert "EROFS" in store.fallback_reason
    assert "storage add-on" in store.fallback_reason


# --------------------------------------------------------------------------- #
#  The boot-time writability probe
# --------------------------------------------------------------------------- #
def test_a_writable_volume_probes_clean_and_leaves_nothing_behind(tmp_path):
    store = GroupStore(tmp_path / "nested" / "groups.json")
    ok, detail = store.writable()
    assert ok is True
    assert detail == "writable, atomic replace (temp file and rename)"
    assert store.strategy == "atomic"
    assert list((tmp_path / "nested").iterdir()) == [], "the probe file must be removed"


def test_the_probe_runs_the_same_mechanism_a_real_save_runs(tmp_path, monkeypatch):
    """The defect this exists to prevent.

    The first probe wrote an empty file with write_text. That took none of the
    steps a save takes, so on an S3-backed mount it reported the volume
    writable while every save on it failed on the fsync. The probe must fail
    wherever a save would fail, and must settle on the mechanism a save will
    then use.
    """
    store = GroupStore(tmp_path / "groups.json")
    monkeypatch.setattr(os, "replace", _refusing(errno.ENOSYS))
    monkeypatch.setattr(os, "fsync", _refusing(errno.ENOSYS))
    ok, detail = store.writable()
    assert ok is True
    assert store.strategy == "direct"
    assert "does not support rename" in detail
    assert "truncated" in detail, "the loss of atomicity has to be stated"
    # And the save that follows uses what the probe settled on.
    store.create(_group())
    assert store.strategy == "direct"
    assert store.persistent


def test_an_unwritable_volume_probes_dirty_with_the_reason(tmp_path, monkeypatch):
    store = GroupStore(tmp_path / "groups.json")
    _block_all_writes(monkeypatch, errno.EROFS)
    ok, detail = store.writable()
    assert ok is False
    assert "EROFS" in detail
    assert "storage add-on" in detail


# --------------------------------------------------------------------------- #
#  Memory fallback: a missing volume degrades persistence, not the application
#
#  A live deployment had no volume at its storage path. Seeding failed, every
#  save returned an error, and the picker, which is the whole point of the
#  change, could not be used at all. The store now keeps the document in
#  process instead. Groups genuinely are lost on restart, and that is reported
#  in three places, but the application works.
# --------------------------------------------------------------------------- #
@pytest.fixture
def memory_store(tmp_path):
    store = GroupStore(tmp_path / "unreachable" / "groups.json")
    store.use_memory_fallback("OSError EROFS (errno 30): Read-only file system.")
    return store


def test_a_store_starts_persistent(tmp_path):
    store = GroupStore(tmp_path / "groups.json")
    assert store.persistent is True
    assert store.fallback_reason is None


def test_the_fallback_reports_itself(memory_store):
    assert memory_store.persistent is False
    assert "EROFS" in memory_store.fallback_reason


def test_seeding_works_in_memory(memory_store):
    assert memory_store.seed_if_empty() == len(SEED_GROUPS)
    assert [g["name"] for g in memory_store.active()] == \
        [g["name"] for g in SEED_GROUPS]


def test_the_full_group_lifecycle_works_in_memory(memory_store):
    created = memory_store.create(_group())
    assert memory_store.get(created["id"])["name"] == created["name"]

    updated = memory_store.update(created["id"], _group(sats=[1, 2, 3], reference=2))
    assert updated["sats"] == [1, 2, 3]
    assert updated["created"] == created["created"]

    memory_store.archive(created["id"])
    assert memory_store.active() == []
    memory_store.restore(created["id"])
    assert [g["id"] for g in memory_store.active()] == [created["id"]]


def test_revisions_still_advance_in_memory(memory_store):
    assert memory_store.load()["rev"] == 0
    created = memory_store.create(_group())
    assert memory_store.load()["rev"] == 1
    memory_store.update(created["id"], _group(sats=[9, 8], reference=9))
    assert memory_store.load()["rev"] == 2


def test_the_stale_revision_check_still_applies_in_memory(memory_store):
    created = memory_store.create(_group())
    stale = memory_store.load()["rev"]
    memory_store.update(created["id"], _group(sats=[1, 2], reference=1))
    mine = _group(sats=[5, 6], reference=5)
    with pytest.raises(ConflictError):
        memory_store.update(created["id"], mine, expected_rev=stale)


def test_validation_still_applies_in_memory(memory_store):
    too_few = _group(sats=[59884])
    with pytest.raises(ValidationError, match="at least"):
        memory_store.create(too_few)


def test_duplicate_names_are_still_refused_in_memory(memory_store):
    memory_store.create(_group())
    same = _group(sats=[1, 2], reference=1)
    with pytest.raises(ValidationError, match="already exists"):
        memory_store.create(same)


def test_nothing_is_written_to_disk_in_memory_mode(memory_store, tmp_path):
    memory_store.seed_if_empty()
    memory_store.create(_group(name="Ephemeral"))
    assert not memory_store.path.exists()
    assert not memory_store.path.parent.exists()


def test_reads_are_snapshots_so_a_caller_cannot_mutate_the_store(memory_store):
    """The file-backed store hands out fresh objects parsed from JSON. The
    memory store must not hand out its own, or a caller editing what it read
    would silently rewrite the store."""
    created = memory_store.create(_group())
    doc = memory_store.load()
    doc["groups"][0]["name"] = "TAMPERED"
    doc["groups"][0]["sats"].append(99999)
    assert memory_store.get(created["id"])["name"] == created["name"]
    assert memory_store.get(created["id"])["sats"] == created["sats"]


def test_engaging_the_fallback_twice_keeps_the_existing_document(memory_store):
    created = memory_store.create(_group())
    memory_store.use_memory_fallback("probed again")
    assert [g["id"] for g in memory_store.active()] == [created["id"]]
    assert memory_store.fallback_reason == "probed again"


def test_the_fallback_is_recorded_as_an_audit_event(tmp_path, caplog):
    import logging
    store = GroupStore(tmp_path / "groups.json")
    with caplog.at_level(logging.INFO, logger="timeslides"):
        store.use_memory_fallback("OSError EROFS (errno 30)")
    records = [r for r in caplog.records
               if r.getMessage() == "storage.memory_fallback"]
    assert len(records) == 1
    assert "EROFS" in records[0].fields["reason"]
