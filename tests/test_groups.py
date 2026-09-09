"""The group store: validation at the boundary, atomic writes, and the
revision check that stops one person's save discarding another's."""

from __future__ import annotations

import json
import os
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
    every save. The message needs to name the cause.

    The failure is injected rather than produced with chmod, because the test
    suite may run as root (it does in the build container) and root ignores
    directory permissions, which would make this test silently pass for the
    wrong reason.
    """
    store = GroupStore(tmp_path / "groups.json")

    def denied(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    payload = _group()
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(ValidationError, match="fsGroup"):
        store.create(payload)


def test_a_failed_write_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    store = GroupStore(tmp_path / "groups.json")
    payload = _group()
    monkeypatch.setattr(os, "replace", _refuse_replace)
    with pytest.raises(ValidationError):
        store.create(payload)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_does_not_damage_an_existing_document(tmp_path, monkeypatch):
    """The whole point of writing to a temporary file and renaming."""
    store = GroupStore(tmp_path / "groups.json")
    first = store.create(_group())
    before = store.path.read_text(encoding="utf-8")
    second = _group(name="SECOND", sats=[1, 2], reference=1)
    monkeypatch.setattr(os, "replace", _refuse_replace)
    with pytest.raises(ValidationError):
        store.create(second)
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == before
    assert [g["id"] for g in store.active()] == [first["id"]]


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
