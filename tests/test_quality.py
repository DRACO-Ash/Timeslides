"""The ingestion data-quality checks.

The distinction these tests exist to hold is between two records from ONE
source at one epoch, which is duplication, and two records from DIFFERENT
sources at one epoch, which is the entire point of the plot. Collapsing the
second would silently delete an independent report.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import numpy as np
import pytest

from timeslides.models import Elset, StateVector
from timeslides.quality import dedupe, dedupe_mixed, summarise

T0 = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)


def _elset(minutes=0, source="18 SDS", line2="2 62903  97.4", created=""):
    return Elset(epoch=T0 + dt.timedelta(minutes=minutes),
                 line1="1 62903U 25001A   26253.29", line2=line2,
                 source=source, created=created)


def _sv(minutes=0, x=7000.0, source="KBR", created=""):
    return StateVector(epoch=T0 + dt.timedelta(minutes=minutes),
                       r=np.array([x, 0.0, 0.0]), v=np.array([0.0, 7.5, 0.0]),
                       source=source, created=created)


# --------------------------------------------------------------------------- #
#  A clean batch stays clean and cheap
# --------------------------------------------------------------------------- #
def test_a_clean_batch_is_returned_untouched_with_no_finding():
    records = [_elset(0), _elset(30), _elset(60)]
    kept, finding = dedupe(records, "18 SDS", 62903)
    assert kept == records
    assert finding is None


def test_an_empty_batch_is_not_a_finding():
    kept, finding = dedupe([], "18 SDS", 62903)
    assert kept == []
    assert finding is None


# --------------------------------------------------------------------------- #
#  Duplicates: same source, same epoch, same values
# --------------------------------------------------------------------------- #
def test_an_identical_record_repeated_is_collapsed_and_counted():
    records = [_elset(0), _elset(0), _elset(30)]
    kept, finding = dedupe(records, "18 SDS", 62903)
    assert len(kept) == 2
    assert [r.epoch for r in kept] == [T0, T0 + dt.timedelta(minutes=30)]
    assert finding["duplicates"] == 1
    assert finding["conflicts"] == []
    assert finding["received"] == 3
    assert finding["plotted"] == 2
    assert finding["source"] == "18 SDS"
    assert finding["satNo"] == 62903


def test_many_copies_of_one_record_count_as_many_duplicates():
    kept, finding = dedupe([_elset(0)] * 5, "18 SDS")
    assert len(kept) == 1
    assert finding["duplicates"] == 4


def test_state_vectors_are_deduplicated_on_their_values_not_just_the_epoch():
    kept, finding = dedupe([_sv(0), _sv(0), _sv(30)], "KBR")
    assert len(kept) == 2
    assert finding["duplicates"] == 1


# --------------------------------------------------------------------------- #
#  Conflicts: same source, same epoch, different values
# --------------------------------------------------------------------------- #
def test_two_different_records_at_one_epoch_are_a_conflict_not_a_duplicate():
    """This is the one that changes the chart, so it is counted separately."""
    kept, finding = dedupe([_elset(0, line2="2 62903  97.4"),
                            _elset(0, line2="2 62903  97.9")], "18 SDS", 62903)
    assert len(kept) == 1
    assert finding["duplicates"] == 0
    assert len(finding["conflicts"]) == 1
    assert finding["conflicts"][0]["epoch"] == "2026-09-10T07:00:00Z"


def test_a_conflict_is_resolved_towards_the_most_recently_created_record():
    """A provider reissuing a correction wants the correction plotted."""
    old = _elset(0, line2="2 62903  97.4", created="2026-09-10T07:01:00Z")
    new = _elset(0, line2="2 62903  97.9", created="2026-09-10T07:05:00Z")
    kept, finding = dedupe([old, new], "18 SDS")
    assert kept[0].line2 == "2 62903  97.9"
    assert finding["conflicts"][0]["arbitrary"] is False
    assert finding["conflicts"][0]["kept"] == "2026-09-10T07:05:00Z"


def test_the_newest_wins_regardless_of_arrival_order():
    """Otherwise the same feed draws a different chart on every run."""
    old = _elset(0, line2="2 62903  97.4", created="2026-09-10T07:01:00Z")
    new = _elset(0, line2="2 62903  97.9", created="2026-09-10T07:05:00Z")
    first, _ = dedupe([old, new], "18 SDS")
    second, _ = dedupe([new, old], "18 SDS")
    assert first[0].line2 == second[0].line2 == "2 62903  97.9"


def test_a_conflict_with_no_creation_stamp_says_the_choice_was_arbitrary():
    """There is nothing to order by, so first seen wins and the finding admits
    that rather than implying the pick meant something."""
    kept, finding = dedupe([_elset(0, line2="2 62903  97.4"),
                            _elset(0, line2="2 62903  97.9")], "18 SDS")
    assert kept[0].line2 == "2 62903  97.4"
    assert finding["conflicts"][0]["arbitrary"] is True
    assert finding["conflicts"][0]["kept"] == "first seen"


def test_two_records_with_the_same_creation_stamp_are_an_arbitrary_pick():
    """A stamp that is present but identical decides nothing.

    Reporting this as resolved by creation time would tell an analyst the pick
    meant something when it did not, which is worse than admitting the tie.
    """
    stamp = "2026-09-10T07:01:00Z"
    kept, finding = dedupe([_elset(0, line2="2 62903  97.4", created=stamp),
                            _elset(0, line2="2 62903  97.9", created=stamp)],
                           "18 SDS")
    assert kept[0].line2 == "2 62903  97.4"
    assert finding["conflicts"][0]["arbitrary"] is True


def test_one_record_with_a_stamp_beats_one_without():
    """A stamp is evidence and no stamp is not, so the stamped record wins and
    the choice is not arbitrary."""
    kept, finding = dedupe([_elset(0, line2="2 62903  97.4"),
                            _elset(0, line2="2 62903  97.9",
                                   created="2026-09-10T07:05:00Z")], "18 SDS")
    assert kept[0].line2 == "2 62903  97.9"
    assert finding["conflicts"][0]["arbitrary"] is False


def test_duplicates_and_conflicts_are_counted_independently():
    records = [_elset(0), _elset(0),                       # a duplicate
               _elset(30, line2="2 62903  97.4"),
               _elset(30, line2="2 62903  97.9")]          # a conflict
    kept, finding = dedupe(records, "18 SDS")
    assert len(kept) == 2
    assert finding["duplicates"] == 1
    assert len(finding["conflicts"]) == 1


# --------------------------------------------------------------------------- #
#  The distinction that matters: two sources are not a duplicate
# --------------------------------------------------------------------------- #
def test_two_sources_at_the_same_epoch_are_both_kept():
    """The element-set query is not filtered by source, so one batch can hold
    several originators. Two independent element sets for one object at one
    epoch is the plot working, not a fault. Collapsing them would delete a
    report the operator is entitled to see."""
    records = [_elset(0, source="18 SDS"), _elset(0, source="KBR")]
    kept, findings = dedupe_mixed(records, "unattributed", 62903)
    assert len(kept) == 2
    assert sorted(r.source for r in kept) == ["18 SDS", "KBR"]
    assert findings == []


def test_each_source_is_deduplicated_only_against_itself():
    records = [_elset(0, source="18 SDS"), _elset(0, source="18 SDS"),
               _elset(0, source="KBR")]
    kept, findings = dedupe_mixed(records, "unattributed")
    assert len(kept) == 2
    assert len(findings) == 1
    assert findings[0]["source"] == "18 SDS"
    assert findings[0]["duplicates"] == 1


def test_records_with_no_source_are_grouped_under_the_fallback_label():
    """The feed did not say, so they cannot be told apart. Saying so is more
    honest than inventing a provenance for them."""
    records = [_elset(0, source=""), _elset(0, source="   ")]
    kept, findings = dedupe_mixed(records, "unattributed", 62903)
    assert len(kept) == 1
    assert findings[0]["source"] == "unattributed"
    assert findings[0]["duplicates"] == 1


def test_a_mixed_batch_comes_back_in_epoch_order():
    """Downstream propagation assumes it."""
    records = [_elset(60, source="KBR"), _elset(0, source="18 SDS"),
               _elset(30, source="KBR")]
    kept, _findings = dedupe_mixed(records, "unattributed")
    assert [r.epoch for r in kept] == sorted(r.epoch for r in records)


# --------------------------------------------------------------------------- #
#  Reporting
# --------------------------------------------------------------------------- #
def test_the_summary_totals_across_every_source_and_object():
    _kept, one = dedupe([_elset(0), _elset(0)], "18 SDS", 62903)
    _kept, two = dedupe([_sv(0, x=7000.0), _sv(0, x=7001.0)], "KBR", 62904)
    total = summarise([one, None, two])
    assert total["sources"] == 2
    assert total["duplicates"] == 1
    assert total["conflicts"] == 1
    assert total["arbitrary"] == 1
    assert len(total["findings"]) == 2


def test_a_run_with_nothing_wrong_summarises_to_zeroes():
    total = summarise([None, None])
    assert total == {"sources": 0, "duplicates": 0, "conflicts": 0,
                     "arbitrary": 0, "findings": []}


def test_a_finding_is_recorded_as_an_audit_event(caplog):
    """The report shows these, but the pod log is the durable record, so both
    carry them."""
    with caplog.at_level(logging.INFO, logger="timeslides"):
        dedupe([_elset(0), _elset(0)], "18 SDS", 62903)
    found = [r for r in caplog.records if r.getMessage() == "quality.duplicates"]
    assert len(found) == 1
    assert found[0].fields["source"] == "18 SDS"
    assert found[0].fields["sat_no"] == 62903
    assert found[0].fields["duplicates"] == 1


def test_a_clean_batch_records_no_audit_event(caplog):
    """A clean feed is the common case and must stay quiet, or the signal is
    lost in the noise."""
    with caplog.at_level(logging.INFO, logger="timeslides"):
        dedupe([_elset(0), _elset(30)], "18 SDS", 62903)
    assert [r for r in caplog.records
            if r.getMessage() == "quality.duplicates"] == []


def test_a_finding_is_json_serialisable():
    """It is embedded in the report's script block, so it has to be."""
    _kept, finding = dedupe([_elset(0, line2="a"), _elset(0, line2="b")], "18 SDS")
    assert json.loads(json.dumps(finding))["conflicts"][0]["arbitrary"] is True


@pytest.mark.parametrize("label", ["18 SDS", "Space-Track", "KBR"])
def test_the_source_label_is_carried_into_the_finding(label):
    _kept, finding = dedupe([_elset(0, source=label), _elset(0, source=label)],
                            label)
    assert finding["source"] == label
