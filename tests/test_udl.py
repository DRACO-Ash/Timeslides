"""The UDL client: request shape, error mapping, and what never reaches a log."""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pytest

from tests.conftest import FakeResponse, sv_record
from timeslides.errors import ConfigError, UpstreamError
from timeslides.udl import UDLClient, elset_to_tle, parse_epoch

START = dt.datetime(2026, 6, 24)
END = dt.datetime(2026, 7, 1)


# --------------------------------------------------------------------------- #
#  Construction
# --------------------------------------------------------------------------- #
def test_the_client_refuses_to_build_without_credentials(tmp_path, instant_bucket):
    from timeslides.config import Settings
    settings = Settings(storage_path=tmp_path)
    session = object()
    with pytest.raises(ConfigError, match="UDL_USER and UDL_PASS"):
        UDLClient(settings, session=session, bucket=instant_bucket)


# --------------------------------------------------------------------------- #
#  Epoch parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [
    ("2026-06-24T12:30:00.000Z", dt.datetime(2026, 6, 24, 12, 30)),
    ("2026-06-24T12:30:00Z", dt.datetime(2026, 6, 24, 12, 30)),
    ("2026-06-24 12:30:00Z", dt.datetime(2026, 6, 24, 12, 30)),
    ("2026-06-24T12:30:00+00:00", dt.datetime(2026, 6, 24, 12, 30)),
])
def test_epoch_formats_all_parse_to_naive_utc(raw, want):
    got = parse_epoch(raw)
    assert got == want
    assert got.tzinfo is None


def test_a_non_utc_offset_is_converted_not_dropped():
    assert parse_epoch("2026-06-24T13:30:00+01:00") == dt.datetime(2026, 6, 24, 12, 30)


@pytest.mark.parametrize("raw", ["not-a-date", "", None, "2026-13-45T99:99:99Z"])
def test_a_malformed_epoch_is_an_upstream_error_naming_the_value(raw):
    with pytest.raises(UpstreamError, match="unparseable epoch"):
        parse_epoch(raw)


# --------------------------------------------------------------------------- #
#  State vectors
# --------------------------------------------------------------------------- #
def test_state_vectors_are_requested_with_the_expected_filters(client):
    c, session = client([[sv_record()]])
    c.state_vectors(59884, START, END, source="LeoLabs", data_mode="REAL")
    call = session.calls[0]
    assert call["url"] == "https://udl.test/udl/statevector"
    assert call["params"]["satNo"] == 59884
    assert call["params"]["source"] == "LeoLabs"
    assert call["params"]["dataMode"] == "REAL"
    assert call["params"]["maxResults"] == 500
    assert call["params"]["epoch"] == ("2026-06-24T00:00:00.000000Z.."
                                       "2026-07-01T00:00:00.000000Z")
    assert call["timeout"] == 60


def test_state_vectors_are_parsed_into_arrays(client):
    c, _ = client([[sv_record(n=2.0)]])
    svs = c.state_vectors(59884, START, END)
    assert len(svs) == 1
    np.testing.assert_array_equal(svs[0].r, [2.0, 4.0, 6.0])
    np.testing.assert_array_equal(svs[0].v, [8.0, 10.0, 12.0])
    assert svs[0].frame == "J2000"


def test_a_record_without_a_reference_frame_falls_back_to_the_provider_frame(client):
    c, _ = client([[sv_record(frame=None)]])
    svs = c.state_vectors(59884, START, END, default_frame="J2000")
    assert svs[0].frame == "J2000"


def test_the_assumed_frame_is_recorded_as_an_audit_event(client, caplog):
    """If the frame assumption is wrong the offsets are wrong, so the
    assumption must be visible in the log, not just on stderr."""
    c, _ = client([[sv_record(frame=None), sv_record(frame="TEME")]])
    with caplog.at_level(logging.INFO, logger="timeslides"):
        c.state_vectors(59884, START, END, source="LeoLabs", default_frame="J2000")
    records = [r for r in caplog.records if r.getMessage() == "udl.statevector.frame_assumed"]
    assert len(records) == 1
    assert records[0].fields["records_without_frame"] == 1
    assert records[0].fields["records_total"] == 2
    assert records[0].fields["assumed_frame"] == "J2000"


def test_no_audit_event_when_every_record_declares_its_frame(client, caplog):
    c, _ = client([[sv_record(frame="TEME")]])
    with caplog.at_level(logging.INFO, logger="timeslides"):
        c.state_vectors(59884, START, END)
    assert not [r for r in caplog.records
                if r.getMessage() == "udl.statevector.frame_assumed"]


# --------------------------------------------------------------------------- #
#  Element sets
# --------------------------------------------------------------------------- #
def test_elsets_use_the_supplied_tle_lines_when_present(client):
    lines = {"epoch": "2026-06-24T00:00:00Z", "line1": "1 X", "line2": "2 X"}
    c, _ = client([[lines]])
    got = c.elsets(59884, START, END)
    assert (got[0].line1, got[0].line2) == ("1 X", "2 X")


def test_elsets_are_rebuilt_from_mean_elements_when_lines_are_absent(client):
    rec = {"epoch": "2026-06-24T00:00:00Z", "satNo": 59884, "eccentricity": 0.0008,
           "inclination": 53.0, "raan": 120.0, "argOfPerigee": 30.0,
           "meanAnomaly": 200.0, "meanMotion": 15.2}
    c, _ = client([[rec]])
    got = c.elsets(59884, START, END)
    assert got[0].line1.startswith("1 59884U")
    assert got[0].line2.startswith("2 59884")
    # A rebuilt element set must be loadable by SGP4, or it is worthless.
    assert got[0].satrec().satnum == 59884


def test_a_rebuilt_tle_carries_a_valid_checksum():
    rec = {"epoch": "2026-06-24T00:00:00Z", "satNo": 25544, "eccentricity": 0.0006317,
           "inclination": 51.64, "raan": 208.9163, "argOfPerigee": 69.9862,
           "meanAnomaly": 290.1789, "meanMotion": 15.49309620}
    from timeslides.udl import _checksum
    for line in elset_to_tle(rec):
        assert int(line[-1]) == _checksum(line)


def test_an_elset_with_neither_lines_nor_elements_is_an_upstream_error(client):
    c, _ = client([[{"epoch": "2026-06-24T00:00:00Z", "satNo": 1}]])
    with pytest.raises(UpstreamError, match="neither TLE lines nor the mean"):
        c.elsets(1, START, END)


# --------------------------------------------------------------------------- #
#  Error mapping. Nothing here may echo a credential or a response body.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,fragment", [
    (401, "rejected the credentials"),
    (403, "rejected the credentials"),
    (429, "rate-limited this app"),
    (500, "HTTP 500"),
    (404, "HTTP 404"),
])
def test_http_failures_map_to_upstream_errors(client, status, fragment):
    c, _ = client([FakeResponse(status)])
    with pytest.raises(UpstreamError, match=fragment):
        c.state_vectors(1, START, END)


def test_a_transport_failure_names_the_exception_type_not_the_url_contents(client):
    c, _ = client(None, raises=OSError("connection reset"))
    with pytest.raises(UpstreamError, match="failed: OSError"):
        c.state_vectors(1, START, END)


def test_a_non_json_body_is_an_upstream_error(client):
    c, _ = client([FakeResponse(200, raise_on_json=True)])
    with pytest.raises(UpstreamError, match="non-JSON"):
        c.state_vectors(1, START, END)


def test_an_unexpected_json_shape_is_an_upstream_error(client):
    c, _ = client([FakeResponse(200, payload="a string")])
    with pytest.raises(UpstreamError, match="unexpected shape"):
        c.state_vectors(1, START, END)


def test_a_wrapped_payload_is_unwrapped(client):
    c, _ = client([FakeResponse(200, payload={"data": [sv_record()]})])
    assert len(c.state_vectors(1, START, END)) == 1


def test_the_password_never_appears_in_an_error_message(client):
    """The credential is 'pw-should-not-appear'. If it turns up in a message it
    turns up in a log, a traceback and possibly an HTTP response."""
    for responses, raises in ([FakeResponse(401)], None), (None, OSError("boom")):
        c, _ = client(responses, raises=raises)
        with pytest.raises(UpstreamError) as exc:
            c.state_vectors(1, START, END)
        assert "pw-should-not-appear" not in str(exc.value)


# --------------------------------------------------------------------------- #
#  Catalogue, for the picker
# --------------------------------------------------------------------------- #
def test_a_numeric_query_is_a_norad_lookup(client):
    c, session = client([[{"satNo": 68762, "name": "ICEYE-X30"}]])
    got = c.search_objects("68762")
    assert got[0]["satNo"] == 68762
    assert got[0]["name"] == "ICEYE-X30"
    assert session.calls[0]["params"]["satNo"] == "68762"


def test_a_text_query_searches_by_name(client):
    c, session = client([[{"satNo": 1, "name": "COSMOS 2581"},
                          {"satNo": 2, "name": "STARLINK-1"}]])
    got = c.search_objects("cosmos")
    assert [r["satNo"] for r in got] == [1]
    assert session.calls[0]["url"] == "https://udl.test/udl/onorbit"


def test_a_tenant_that_ignores_the_name_filter_still_returns_something(client):
    """If the name parameter is unsupported the tenant returns an unfiltered
    page. Returning that beats returning nothing, so the picker still works."""
    c, _ = client([[{"satNo": 1, "name": "ALPHA"}, {"satNo": 2, "name": "BETA"}]])
    got = c.search_objects("zzz-no-match")
    assert [r["satNo"] for r in got] == [1, 2]


def test_an_empty_query_makes_no_request(client):
    c, session = client([])
    assert c.search_objects("   ") == []
    assert session.calls == []


def test_alternative_field_spellings_are_accepted(client):
    """Tenants disagree about field names. ONORBIT_FIELDS lists the aliases."""
    c, _ = client([[{"noradCatId": "44713", "satName": "STARLINK-1007",
                     "internationalDesignator": "2019-074A", "origin": "US"}]])
    got = c.objects_by_satno([44713])
    assert got[44713]["name"] == "STARLINK-1007"
    assert got[44713]["intlDes"] == "2019-074A"
    assert got[44713]["country"] == "US"


def test_records_without_a_usable_satno_are_dropped_not_guessed(client):
    c, _ = client([[{"name": "MYSTERY"}, {"satNo": "not-a-number", "name": "X"},
                    {"satNo": 7, "name": "GOOD"}]])
    got = c._onorbit({})
    assert [r["satNo"] for r in got] == [7]


def test_a_record_without_a_name_gets_a_placeholder_not_a_crash(client):
    c, _ = client([[{"satNo": 99}]])
    assert c.objects_by_satno([99])[99]["name"] == "OBJECT 99"


def test_objects_by_satno_batches_into_one_request(client):
    c, session = client([[{"satNo": 1, "name": "A"}, {"satNo": 2, "name": "B"}]])
    got = c.objects_by_satno([2, 1, 1])
    assert set(got) == {1, 2}
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["satNo"] == "1,2"


def test_objects_by_satno_with_no_ids_makes_no_request(client):
    c, session = client([])
    assert c.objects_by_satno([]) == {}
    assert session.calls == []


def test_unrequested_objects_in_the_response_are_discarded(client):
    """A tenant that over-returns must not silently widen the result."""
    c, _ = client([[{"satNo": 1, "name": "A"}, {"satNo": 999, "name": "UNASKED"}]])
    assert set(c.objects_by_satno([1])) == {1}


# --------------------------------------------------------------------------- #
#  Rate limiting
# --------------------------------------------------------------------------- #
def test_every_request_takes_a_token(client, settings):
    from timeslides.ratelimit import TokenBucket
    now = [0.0]
    bucket = TokenBucket(60, clock=lambda: now[0], sleep=lambda d: now.__setitem__(0, now[0] + d))
    from timeslides.udl import UDLClient
    from tests.conftest import FakeSession
    c = UDLClient(settings, session=FakeSession([[], [], []]), bucket=bucket)
    before = bucket.tokens
    c.elsets(1, START, END)
    c.elsets(2, START, END)
    assert bucket.tokens == pytest.approx(before - 2)


# --------------------------------------------------------------------------- #
#  Provider availability
# --------------------------------------------------------------------------- #
def test_a_provider_that_answers_is_reported_available(client):
    from timeslides.models import STATE_SOURCES
    c, session = client([[sv_record()]])
    got = c.probe_source(STATE_SOURCES[0], 59884, START, END)
    assert got["available"] is True
    assert got["records"] == 1
    assert got["error"] is None
    assert got["udlSource"] == "LeoLabs"
    assert session.calls[0]["params"]["maxResults"] == 1


def test_a_provider_that_returns_nothing_is_reported_unavailable(client):
    """The failure this exists to catch: a source string the tenant spells
    differently returns an empty list, not an error, so the provider silently
    never appears in a report."""
    from timeslides.models import STATE_SOURCES
    c, _ = client([[]])
    got = c.probe_source(STATE_SOURCES[3], 59884, START, END)
    assert got["available"] is False
    assert got["records"] == 0
    assert got["error"] is None
    assert got["label"] == "PPEC"


def test_a_provider_that_errors_reports_the_reason_rather_than_raising(client):
    """One bad provider must not sink the whole probe."""
    from timeslides.models import STATE_SOURCES
    c, _ = client([FakeResponse(401)])
    got = c.probe_source(STATE_SOURCES[0], 59884, START, END)
    assert got["available"] is False
    assert "rejected the credentials" in got["error"]


def test_probing_covers_every_configured_provider_with_one_request_each(client):
    from timeslides.models import STATE_SOURCES
    c, session = client([[sv_record()], [], [], [sv_record()], []])
    got = c.probe_sources(59884, START, END)
    assert [r["key"] for r in got] == [s["key"] for s in STATE_SOURCES]
    assert len(session.calls) == len(STATE_SOURCES)
    assert [r["available"] for r in got] == [True, False, False, True, False]


def test_probing_uses_each_providers_own_udl_source_string(client):
    from timeslides.models import STATE_SOURCES
    c, session = client([[]] * len(STATE_SOURCES))
    c.probe_sources(59884, START, END)
    assert [call["params"]["source"] for call in session.calls] == \
        [s["udl_source"] for s in STATE_SOURCES]


def test_probe_results_are_recorded_as_an_audit_event(client, caplog):
    c, _ = client([[sv_record()], [], [], [], []])
    with caplog.at_level(logging.INFO, logger="timeslides"):
        c.probe_sources(59884, START, END)
    rec = [r for r in caplog.records if r.getMessage() == "sources.probed"]
    assert len(rec) == 1
    assert rec[0].fields["available"] == "leolabs"
    assert "ppec" in rec[0].fields["missing"]


def test_space_track_is_now_a_state_vector_provider(client):
    """It used to be the element-set series' label. It is a provider."""
    from timeslides.models import STATE_SOURCES
    st = next(s for s in STATE_SOURCES if s["key"] == "spacetrack")
    c, session = client([[sv_record()]])
    c.probe_source(st, 59884, START, END)
    assert session.calls[0]["url"].endswith("/udl/statevector")
    assert session.calls[0]["params"]["source"] == "Space-Track"
