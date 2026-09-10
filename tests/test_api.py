"""The HTTP surface, in-process. No network, no volume outside tmp_path."""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from timeslides.api import _matches_etag, create_app
from timeslides.config import Settings
from timeslides.groups import GroupStore
from timeslides.jobs import DONE, FAILED, JobRunner
from timeslides.pipeline import MAX_WINDOW_DAYS


@pytest.fixture
def app_bits(tmp_path):
    """A real app with a stub renderer, so tests never touch the UDL and never
    spend twenty seconds building a real report."""
    settings = Settings(udl_user="u", udl_pass="secret-pw", storage_path=tmp_path,
                        classification="OFFICIAL")
    store = GroupStore(settings.groups_file)
    rendered = []

    def render(spec, progress):
        rendered.append(spec)
        progress("group", 1, 1)
        return "<!DOCTYPE html><html><body>REPORT</body></html>"

    runner = JobRunner(render, settings.runs_path, workers=1)
    app = create_app(settings=settings, store=store, runner=runner,
                     client_factory=FakeCatalogue)
    yield app, store, runner, rendered
    runner.shutdown()


class FakeCatalogue:
    def search_objects(self, q, limit=50):
        return [dict(satNo=59884, name="OBJECT G")] if "g" in q.lower() else []

    def objects_by_satno(self, sat_nos):
        return {n: dict(satNo=n, name=f"OBJECT {n}") for n in sat_nos}


@pytest.fixture
def client(app_bits):
    app, _, _, _ = app_bits
    with TestClient(app) as c:
        yield c


def _await_run(client, run_id, timeout=60.0):
    """Wait on a deadline, not on an iteration count.

    This used to busy-poll a fixed 200 times with no wait, which took well
    under a second in total. That was long enough for the stub renderer and not
    long enough for a real one, so the test that drives the app's own renderer
    failed whenever the render was slower, including under coverage. It would
    have failed the platform's test stage for the same reason.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["status"] in (DONE, FAILED):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# --------------------------------------------------------------------------- #
#  The readiness target
# --------------------------------------------------------------------------- #
def test_the_root_path_returns_200_html(client):
    """The App Store probes port 8080 path / and wants a 200."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in r.text


def test_the_root_path_makes_no_upstream_call(app_bits):
    """A readiness probe that depends on the UDL turns their outage into our
    restart loop, so / must not build a client at all."""
    settings, store = app_bits[0].state.settings, app_bits[1]

    def explode():
        raise AssertionError("the index page built a UDL client")

    app = create_app(settings=settings, store=store, runner=app_bits[2],
                     client_factory=explode)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/healthz").status_code == 200


def test_the_shell_carries_both_tabs_and_the_classification(client):
    body = client.get("/").text
    assert 'data-tab="cfg"' in body
    assert 'data-tab="rep"' in body
    assert "OFFICIAL" in body


def test_healthz_reports_the_mode_and_the_storage_state(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["demo"] is False
    assert set(body["storage"]) == {"writable", "mode", "strategy",
                                    "detail", "path"}


def test_the_api_schema_is_not_published(client):
    """Nothing here needs an interactive schema browser exposed."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------------- #
#  Groups
# --------------------------------------------------------------------------- #
def test_the_seeded_groups_are_present_on_first_boot(client):
    body = client.get("/api/groups").json()
    assert [g["name"] for g in body["groups"]] == [
        "SPIDER BABIES w/ICEYE", "COSMOS 2581/82/83", "PRC SpacePlane 4"]
    assert body["rev"] >= 1


def test_a_group_can_be_created_from_selections(client):
    r = client.post("/api/groups", json={"name": "New Watch",
                                         "sats": [40001, 40002], "reference": 40001})
    assert r.status_code == 201
    assert r.json()["name"] == "New Watch"
    assert "New Watch" in [g["name"] for g in client.get("/api/groups").json()["groups"]]


def test_a_group_can_be_updated(client):
    gid = client.get("/api/groups").json()["groups"][0]["id"]
    r = client.put(f"/api/groups/{gid}", json={"name": "Renamed",
                                               "sats": [1, 2, 3], "reference": 2})
    assert r.status_code == 200
    assert r.json()["sats"] == [1, 2, 3]
    assert r.json()["reference"] == 2


def test_archiving_hides_a_group_and_restoring_brings_it_back(client):
    gid = client.get("/api/groups").json()["groups"][0]["id"]
    assert client.delete(f"/api/groups/{gid}").status_code == 200
    assert gid not in [g["id"] for g in client.get("/api/groups").json()["groups"]]
    assert gid in [g["id"] for g in
                   client.get("/api/groups?includeArchived=true").json()["groups"]]
    assert client.post(f"/api/groups/{gid}/restore").status_code == 200
    assert gid in [g["id"] for g in client.get("/api/groups").json()["groups"]]


@pytest.mark.parametrize("payload,status", [
    ({"name": "X", "sats": [1], "reference": 1}, 400),            # too few objects
    ({"name": "X", "sats": [1, 2], "reference": 99}, 400),        # ref not a member
    ({"name": "", "sats": [1, 2]}, 422),                          # empty name
    ({"sats": [1, 2]}, 422),                                      # no name
    ({"name": "X"}, 422),                                         # no sats
    ({"name": "X", "sats": ["abc", "def"]}, 400),                 # not numbers
    ({"name": "X", "sats": []}, 422),                             # empty list
])
def test_bad_group_payloads_are_rejected_at_the_boundary(client, payload, status):
    assert client.post("/api/groups", json=payload).status_code == status


def test_a_duplicate_name_is_a_400_with_an_explanation(client):
    r = client.post("/api/groups", json={"name": "PRC SpacePlane 4",
                                         "sats": [1, 2], "reference": 1})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_an_unknown_group_id_is_a_404(client):
    assert client.put("/api/groups/nope", json={"name": "X", "sats": [1, 2]}
                      ).status_code == 404
    assert client.delete("/api/groups/nope").status_code == 404


def test_a_stale_revision_is_a_409(client):
    body = client.get("/api/groups").json()
    gid, stale = body["groups"][0]["id"], body["rev"]
    client.put(f"/api/groups/{gid}", json={"name": "A", "sats": [1, 2], "reference": 1})
    r = client.put(f"/api/groups/{gid}",
                   json={"name": "B", "sats": [3, 4], "reference": 3, "rev": stale})
    assert r.status_code == 409
    assert "changed since you loaded it" in r.json()["detail"]


def test_the_error_body_names_the_error_type(client):
    r = client.post("/api/groups", json={"name": "X", "sats": [1], "reference": 1})
    assert r.json()["error"] == "ValidationError"
    assert r.json()["detail"]


# --------------------------------------------------------------------------- #
#  Catalogue
# --------------------------------------------------------------------------- #
def test_the_catalogue_proxies_the_udl_search(client):
    body = client.get("/api/catalogue", params={"q": "object g"}).json()
    assert body["results"][0]["satNo"] == 59884
    assert body["demo"] is False


def test_an_over_long_catalogue_query_is_rejected(client):
    assert client.get("/api/catalogue", params={"q": "x" * 200}).status_code == 422


@pytest.mark.parametrize("limit", [0, 201, -5])
def test_an_out_of_range_catalogue_limit_is_rejected(client, limit):
    assert client.get("/api/catalogue",
                      params={"q": "g", "limit": limit}).status_code == 422


def test_demo_mode_serves_a_synthetic_catalogue(tmp_path):
    settings = Settings(storage_path=tmp_path, demo=True)
    app = create_app(settings=settings, store=GroupStore(tmp_path / "g.json"),
                     runner=JobRunner(lambda s, p: "<html/>", tmp_path / "runs"))
    with TestClient(app) as c:
        body = c.get("/api/catalogue", params={"q": "cosmos"}).json()
        assert body["demo"] is True
        assert [r["satNo"] for r in body["results"]] == [62902, 62903, 62904]


# --------------------------------------------------------------------------- #
#  Runs
# --------------------------------------------------------------------------- #
def test_a_run_is_accepted_and_completes(client):
    r = client.post("/api/runs", json={"days": 7})
    assert r.status_code == 202
    body = _await_run(client, r.json()["id"])
    assert body["status"] == DONE
    assert body["reportUrl"]


def test_a_run_covers_every_live_group_when_none_is_named(client, app_bits):
    _, _, _, rendered = app_bits
    client.post("/api/runs", json={"days": 7})
    _await_run(client, client.get("/api/runs").json()["runs"][0]["id"])
    assert len(rendered[0].group_ids) == 3


def test_a_run_can_name_specific_groups(client, app_bits):
    _, _, _, rendered = app_bits
    gid = client.get("/api/groups").json()["groups"][1]["id"]
    r = client.post("/api/runs", json={"groupIds": [gid]})
    _await_run(client, r.json()["id"])
    assert rendered[0].group_ids == (gid,)


def test_an_identical_run_joins_rather_than_re_rendering(client, app_bits):
    _, _, _, rendered = app_bits
    first = client.post("/api/runs", json={"days": 7}).json()
    _await_run(client, first["id"])
    posted = client.post("/api/runs", json={"days": 7})
    # Assert the status before reading the body: a 429 from the rate limiter
    # would otherwise surface as a bare KeyError on "joined" and read as a
    # broken join rather than a throttled request.
    assert posted.status_code == 202, posted.text
    second = posted.json()
    assert second["joined"] is True
    assert second["id"] == first["id"]
    assert len(rendered) == 1


def test_rendering_an_archived_group_is_refused(client):
    gid = client.get("/api/groups").json()["groups"][0]["id"]
    client.delete(f"/api/groups/{gid}")
    r = client.post("/api/runs", json={"groupIds": [gid]})
    assert r.status_code == 400
    assert "archived" in r.json()["detail"]


def test_a_run_with_no_groups_defined_explains_where_to_make_one(client):
    for group in client.get("/api/groups").json()["groups"]:
        client.delete(f"/api/groups/{group['id']}")
    r = client.post("/api/runs", json={})
    assert r.status_code == 400
    assert "Configure tab" in r.json()["detail"]


@pytest.mark.parametrize("payload", [
    {"days": 0}, {"days": MAX_WINDOW_DAYS + 1}, {"days": -1},
    {"modes": ["REAL"] * 9}, {"groupIds": ["x"] * 20},
])
def test_bad_run_payloads_are_rejected(client, payload):
    assert client.post("/api/runs", json=payload).status_code == 422


def test_an_unknown_mode_is_a_400(client):
    r = client.post("/api/runs", json={"modes": ["PRETEND"]})
    assert r.status_code == 400
    assert "unknown data mode" in r.json()["detail"]


def test_an_unknown_provider_is_a_400(client):
    r = client.post("/api/runs", json={"sources": ["acme"]})
    assert r.status_code == 400
    assert "unknown state provider" in r.json()["detail"]


def test_an_inverted_window_is_a_400(client):
    r = client.post("/api/runs", json={"start": "2026-07-01T00:00:00Z",
                                       "end": "2026-06-24T00:00:00Z"})
    assert r.status_code == 400
    assert "after its start" in r.json()["detail"]


def test_an_explicit_window_is_honoured_and_normalised_to_utc(client, app_bits):
    _, _, _, rendered = app_bits
    r = client.post("/api/runs", json={"start": "2026-06-24T01:00:00+01:00",
                                       "end": "2026-07-01T00:00:00Z"})
    _await_run(client, r.json()["id"])
    assert rendered[0].start == dt.datetime(2026, 6, 24, 0, 0)
    assert rendered[0].start.tzinfo is None


def test_the_run_carries_the_configured_classification(client, app_bits):
    _, _, _, rendered = app_bits
    r = client.post("/api/runs", json={})
    _await_run(client, r.json()["id"])
    assert rendered[0].classification == "OFFICIAL"


def test_an_unknown_run_id_is_a_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/report").status_code == 404


# --------------------------------------------------------------------------- #
#  The report response
# --------------------------------------------------------------------------- #
def test_the_report_is_served_with_an_etag(client):
    run_id = client.post("/api/runs", json={}).json()["id"]
    _await_run(client, run_id)
    r = client.get(f"/api/runs/{run_id}/report")
    assert r.status_code == 200
    assert "REPORT" in r.text
    assert r.headers["etag"] == f'"{run_id}"'
    assert "phase_offset_" in r.headers["content-disposition"]


def test_a_revisit_gets_a_304_rather_than_the_whole_document(client):
    """The report is multi-megabyte. Without this, every reload of the Report
    tab pushed another four megabytes down the wire."""
    run_id = client.post("/api/runs", json={}).json()["id"]
    _await_run(client, run_id)
    r = client.get(f"/api/runs/{run_id}/report",
                   headers={"If-None-Match": f'"{run_id}"'})
    assert r.status_code == 304
    assert r.content == b""


def test_asking_for_a_report_before_it_is_ready_says_so(app_bits):
    import threading
    app, store, _, _ = app_bits
    release = threading.Event()
    runner = JobRunner(lambda s, p: (release.wait(5), "<html/>")[1],
                       app.state.settings.runs_path, workers=1)
    slow = create_app(settings=app.state.settings, store=store, runner=runner,
                      client_factory=FakeCatalogue)
    try:
        with TestClient(slow) as c:
            run_id = c.post("/api/runs", json={}).json()["id"]
            r = c.get(f"/api/runs/{run_id}/report")
            assert r.status_code == 400
            assert "not ready yet" in r.json()["detail"]
    finally:
        release.set()
        runner.shutdown()


def test_a_failed_run_reports_its_error(app_bits):
    from timeslides.errors import ComputeError
    app, store, _, _ = app_bits

    def boom(spec, progress):
        raise ComputeError("no usable data in the window")

    runner = JobRunner(boom, app.state.settings.runs_path, workers=1)
    failing = create_app(settings=app.state.settings, store=store, runner=runner,
                         client_factory=FakeCatalogue)
    try:
        with TestClient(failing) as c:
            run_id = c.post("/api/runs", json={}).json()["id"]
            body = _await_run(c, run_id)
            assert body["status"] == FAILED
            assert "no usable data" in body["error"]
            assert body["reportUrl"] is None
    finally:
        runner.shutdown()


def test_run_listing_is_newest_first(client):
    first = client.post("/api/runs", json={"days": 7}).json()["id"]
    _await_run(client, first)
    second = client.post("/api/runs", json={"days": 8}).json()["id"]
    _await_run(client, second)
    assert [r["id"] for r in client.get("/api/runs").json()["runs"]][:2] == [second, first]


# --------------------------------------------------------------------------- #
#  Secrets must not escape through the HTTP surface
# --------------------------------------------------------------------------- #
def test_no_response_body_contains_the_credential(client):
    """The fixture password is 'secret-pw'. It must not surface anywhere."""
    paths = ["/", "/healthz", "/api/groups", "/api/runs",
             "/api/catalogue?q=g", "/api/runs/nope"]
    for path in paths:
        assert "secret-pw" not in client.get(path).text, path
    assert "secret-pw" not in client.post(
        "/api/groups", json={"name": "X", "sats": [1]}).text


# --------------------------------------------------------------------------- #
#  ETag matching
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("header,want", [
    (None, False), ("", False), ('"abc"', True), ('W/"abc"', True),
    ('"x", "abc"', True), ("*", True), ('"other"', False), ('"ABC"', False),
])
def test_if_none_match_parsing(header, want):
    assert _matches_etag(header, '"abc"') is want


# --------------------------------------------------------------------------- #
#  Sources
# --------------------------------------------------------------------------- #
def test_the_source_list_carries_all_five_providers_plus_the_element_sets(client):
    body = client.get("/api/sources").json()
    keys = [s["key"] for s in body["sources"]]
    assert keys == ["leolabs", "northstar", "kbr", "ppec", "spacetrack", "elset"]
    assert body["stateKeys"] == ["leolabs", "northstar", "kbr", "ppec", "spacetrack"]
    assert body["elsetKey"] == "elset"


def test_each_source_declares_the_marker_shape_the_plot_will_use(client):
    body = client.get("/api/sources").json()
    shapes = {s["key"]: s["shape"] for s in body["sources"]}
    assert len(set(shapes.values())) == len(shapes), "two sources share a shape"
    assert shapes["ppec"] == "mk-cross"
    assert shapes["spacetrack"] == "mk-ex"
    assert shapes["elset"] == "mk-square"


def test_the_element_set_entry_is_marked_as_not_a_provider(client):
    body = client.get("/api/sources").json()
    kinds = {s["key"]: s["kind"] for s in body["sources"]}
    assert kinds["elset"] == "elset"
    assert kinds["spacetrack"] == "state"
    elset = next(s for s in body["sources"] if s["key"] == "elset")
    assert elset["udlSource"] is None


def test_the_source_list_needs_no_udl_call(app_bits):
    app, store, runner, _ = app_bits

    def explode():
        raise AssertionError("listing the sources built a UDL client")

    listing = create_app(settings=app.state.settings, store=store, runner=runner,
                         client_factory=explode)
    with TestClient(listing) as c:
        assert c.get("/api/sources").status_code == 200


def test_the_probe_reports_which_providers_answered(app_bits):
    app, store, runner, _ = app_bits  # store is read for the probe subject

    class Probing:
        def probe_sources(self, sat_no, start, end):
            return [dict(key="leolabs", label="LeoLabs", udlSource="LeoLabs",
                         available=True, records=1, error=None),
                    dict(key="ppec", label="PPEC", udlSource="PPEC",
                         available=False, records=0, error=None)]

    probing = create_app(settings=app.state.settings, store=store, runner=runner,
                         client_factory=Probing)
    with TestClient(probing) as c:
        body = c.get("/api/sources/probe").json()
        assert body["demo"] is False
        assert [r["available"] for r in body["results"]] == [True, False]
        # Probed with a real object: the first live group's reference.
        assert body["satNo"] == store.active()[0]["reference"]


def test_the_probe_accepts_an_explicit_satellite(app_bits):
    app, store, runner, _ = app_bits
    seen = {}

    class Probing:
        def probe_sources(self, sat_no, start, end):
            seen["satNo"] = sat_no
            return []

    probing = create_app(settings=app.state.settings, store=store, runner=runner,
                         client_factory=Probing)
    with TestClient(probing) as c:
        c.get("/api/sources/probe", params={"satNo": 25544})
        assert seen["satNo"] == 25544


def test_the_probe_with_no_groups_and_no_satellite_says_what_it_needs(client):
    for group in client.get("/api/groups").json()["groups"]:
        client.delete(f"/api/groups/{group['id']}")
    r = client.get("/api/sources/probe")
    assert r.status_code == 400
    assert "nothing to probe with" in r.json()["detail"]


@pytest.mark.parametrize("params", [{"satNo": 0}, {"satNo": 10**10}, {"days": 0},
                                    {"days": 999}])
def test_bad_probe_parameters_are_rejected(client, params):
    assert client.get("/api/sources/probe", params=params).status_code == 422


def test_demo_mode_reports_every_provider_as_available(tmp_path):
    settings = Settings(storage_path=tmp_path, demo=True)
    app = create_app(settings=settings, store=GroupStore(tmp_path / "g.json"),
                     runner=JobRunner(lambda s, p: "<html/>", tmp_path / "runs"))
    with TestClient(app) as c:
        body = c.get("/api/sources/probe").json()
        assert body["demo"] is True
        assert all(r["available"] for r in body["results"])
        assert len(body["results"]) == 5


def test_the_configure_tab_offers_exactly_the_providers_the_report_can_plot(client):
    """The bug this replaced: the Configure tab listed state providers while the
    report legend also carried the element-set series, so the two disagreed.
    The tab's chips are now rendered from the same table the report uses."""
    shell = client.get("/").text
    body = client.get("/api/sources").json()
    for source in body["sources"]:
        if source["kind"] != "state":
            continue
        assert f'data-source="{source["key"]}"' in shell, source["key"]
        assert source["shape"] in shell, source["key"]
    # The element-set series is not offered as a toggle, because it is always
    # plotted and it anchors the reference orbit.
    assert 'data-source="elset"' not in shell


# --------------------------------------------------------------------------- #
#  The paths the injected fakes normally bypass
#
#  The fixtures above inject a renderer and a client so the suite never touches
#  the UDL. That leaves create_app's own wiring unexercised, and the quality
#  gate counts those lines like any other. These tests drive the real wiring
#  with a stub client instead of a stub renderer.
# --------------------------------------------------------------------------- #
def test_naive_and_aware_timestamps_both_normalise_to_naive_utc():
    from timeslides.api import _naive_utc
    naive = dt.datetime(2026, 6, 24, 12, 30, 0, 123456)
    assert _naive_utc(naive) == dt.datetime(2026, 6, 24, 12, 30)
    aware = dt.datetime(2026, 6, 24, 13, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert _naive_utc(aware) == dt.datetime(2026, 6, 24, 12, 30)
    assert _naive_utc(aware).tzinfo is None


def test_the_app_builds_a_real_udl_client_when_none_is_injected(tmp_path, monkeypatch):
    """create_app's default client factory imports and constructs UDLClient.
    Verified with the class stubbed, so no session and no network."""
    built = []

    class StubClient:
        def __init__(self, settings):
            built.append(settings)

        def search_objects(self, q, limit=50):
            return [{"satNo": 1, "name": "STUB"}]

    monkeypatch.setattr("timeslides.udl.UDLClient", StubClient)
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    app = create_app(settings=settings, store=GroupStore(settings.groups_file),
                     runner=JobRunner(lambda s, p: "<html/>", settings.runs_path))
    with TestClient(app) as c:
        body = c.get("/api/catalogue", params={"q": "anything"}).json()
    assert body["results"][0]["name"] == "STUB"
    assert built and built[0] is settings


def test_the_apps_own_renderer_fetches_groups_and_builds_the_report(tmp_path, monkeypatch):
    """The non-demo render path: look the groups up in the store, then build.
    Driven with the pipeline's fake client, so still no network."""
    from tests.test_pipeline import FakeUDL

    monkeypatch.setattr("timeslides.udl.UDLClient", lambda settings: FakeUDL())
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path,
                        classification="OFFICIAL")
    store = GroupStore(settings.groups_file)
    app = create_app(settings=settings, store=store)
    try:
        with TestClient(app) as c:
            # A group whose objects the fake client actually knows about.
            gid = c.post("/api/groups", json={"name": "Fixture Group",
                                              "sats": [59884, 67689, 69673],
                                              "reference": 59884}).json()["id"]
            run_id = c.post("/api/runs", json={"groupIds": [gid]}).json()["id"]
            body = _await_run(c, run_id)
            assert body["status"] == DONE, body.get("error")
            report = c.get(f"/api/runs/{run_id}/report").text
            assert "Fixture Group" in report
            assert "OFFICIAL" in report
    finally:
        app.state.runner.shutdown()


def test_a_store_that_cannot_be_seeded_logs_and_lets_the_app_start(tmp_path, capsys):
    """An unwritable volume must not stop the app serving. The operator meets
    the same problem with a clear message on their first save.

    Read from stdout rather than caplog: create_app calls audit.configure,
    which installs its own handler and sets propagate to False, so caplog never
    sees the record. Parsing the emitted line also asserts the real output.
    """
    from timeslides.errors import ValidationError

    class UnwritableStore(GroupStore):
        def seed_if_empty(self, groups=None):
            raise ValidationError("could not write the group store: fsGroup")

    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    app = create_app(settings=settings,
                     store=UnwritableStore(settings.groups_file),
                     runner=JobRunner(lambda s, p: "<html/>", settings.runs_path))
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/healthz").status_code == 200
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()
             if line.startswith("{")]
    failures = [line for line in lines if line["msg"] == "boot.seed_failed"]
    assert len(failures) == 1
    assert failures[0]["error"] == "ValidationError"
    assert "fsGroup" in failures[0]["detail"]


def test_the_apps_own_renderer_serves_demo_data_in_demo_mode(tmp_path):
    """The demo branch of the app's own renderer.

    Covered locally by the browser suite, which builds the app with no runner
    override. That suite skips wherever no browser is provisioned, including
    the platform's test stage, so this line read as uncovered there. Same path,
    no browser.
    """
    settings = Settings(storage_path=tmp_path, demo=True, classification="OFFICIAL")
    app = create_app(settings=settings, store=GroupStore(settings.groups_file))
    try:
        with TestClient(app) as c:
            run_id = c.post("/api/runs", json={"days": 7}).json()["id"]
            body = _await_run(c, run_id, timeout=180.0)
            assert body["status"] == DONE, body.get("error")
            report = c.get(f"/api/runs/{run_id}/report").text
            assert "PRC Spaceplane" in report
            assert "OFFICIAL" in report
    finally:
        app.state.runner.shutdown()


# --------------------------------------------------------------------------- #
#  Storage state: visible at boot, on /healthz and in the page
# --------------------------------------------------------------------------- #
def test_a_writable_volume_is_reported_as_such(client):
    body = client.get("/healthz").json()
    assert body["storage"]["writable"] is True
    assert body["storage"]["detail"].startswith("writable")
    assert body["storage"]["strategy"] == "atomic"
    assert body["storage"]["path"].endswith("groups.json")


def test_the_page_carries_no_warning_when_the_volume_is_writable(client):
    assert "kept in memory" not in client.get("/").text


class _NoPosixOS:
    """os, as an S3-backed FUSE mount presents it: no fsync, no rename.

    Patched over timeslides.groups.os so the simulation stays inside the store
    and cannot disturb pytest's or coverage's own file writing. This is the
    mount the App Store actually provides: the File Storage add-on is S3-backed
    and mounted at /data, and assuming POSIX there made every save fail with
    ENOSYS.
    """

    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, *_args, **_kwargs):
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    def fsync(self, *_args, **_kwargs):
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))


def test_groups_save_to_an_s3_backed_mount_and_stay_on_it(tmp_path, monkeypatch):
    """The deployment as it actually is. This is the failure the operator saw:
    ENOSYS (errno 38), Function not implemented, on every save.

    The assertion that matters is the last one. The group has to be on the
    volume, not merely accepted, because a group that only reaches memory does
    not survive the restart the storage add-on was enabled to survive.
    """
    monkeypatch.setattr("timeslides.groups.os", _NoPosixOS())
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    app = create_app(settings=settings, store=GroupStore(settings.groups_file),
                     runner=JobRunner(lambda s, p: "<html/>", settings.runs_path))
    with TestClient(app) as c:
        storage = c.get("/healthz").json()["storage"]
        assert storage["writable"] is True
        assert storage["mode"] == "volume"
        assert storage["strategy"] == "direct"

        made = c.post("/api/groups", json={"name": "COSMOS Triplet",
                                           "sats": [62902, 62903, 62904],
                                           "reference": 62902})
        assert made.status_code == 201, made.text
        names = [g["name"] for g in c.get("/api/groups").json()["groups"]]
        assert "COSMOS Triplet" in names

    on_disk = json.loads((tmp_path / "groups.json").read_text(encoding="utf-8"))
    assert "COSMOS Triplet" in [g["name"] for g in on_disk["groups"]]


def test_an_s3_backed_mount_survives_a_restart(tmp_path, monkeypatch):
    """A second app on the same volume reads back what the first one saved.
    That is the whole reason the storage add-on is attached."""
    monkeypatch.setattr("timeslides.groups.os", _NoPosixOS())
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)

    def build():
        return create_app(settings=settings, store=GroupStore(settings.groups_file),
                          runner=JobRunner(lambda s, p: "<html/>", settings.runs_path))

    with TestClient(build()) as first:
        first.post("/api/groups", json={"name": "Survivor", "sats": [1, 2],
                                        "reference": 1})
    with TestClient(build()) as second:
        assert second.get("/healthz").json()["storage"]["mode"] == "volume"
        names = [g["name"] for g in second.get("/api/groups").json()["groups"]]
    assert "Survivor" in names


def _unwritable_app(tmp_path, rendered=None):
    """A store whose volume refuses writes, the way an unmounted or
    root-owned /data does. Nothing else is stubbed: the app is left to cope
    with it exactly as it would in the cluster.

    Pass `rendered` to capture the specs the runner is handed, so a test can
    assert what actually reached the renderer.
    """

    class UnwritableStore(GroupStore):
        def writable(self):
            return False, ("OSError EROFS (errno 30): Read-only file system. the "
                           "filesystem is read-only, which usually means no storage "
                           "volume is mounted here at all.")

    def render(spec, progress):
        if rendered is not None:
            rendered.append(spec)
        return "<!DOCTYPE html><html><body>REPORT</body></html>"

    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    return create_app(settings=settings, store=UnwritableStore(settings.groups_file),
                      runner=JobRunner(render, settings.runs_path))


def test_an_unwritable_volume_is_reported_without_failing_readiness(tmp_path):
    """Readiness is about whether this pod can serve, and it can: groups fall
    back to memory and the report path never needed the volume. So the probe
    target and health both stay 200 and the problem is reported in the body."""
    with TestClient(_unwritable_app(tmp_path)) as c:
        assert c.get("/").status_code == 200
        health = c.get("/healthz")
        assert health.status_code == 200
        assert health.json()["storage"]["writable"] is False
        assert health.json()["storage"]["mode"] == "memory"
        assert "EROFS" in health.json()["storage"]["detail"]


def test_a_writable_volume_reports_volume_mode(client):
    assert client.get("/healthz").json()["storage"]["mode"] == "volume"


def test_groups_can_still_be_created_and_listed_with_no_volume(tmp_path):
    """The core operation. Adding and saving a group is the whole point of the
    application, so it must not depend on an add-on somebody forgot to attach."""
    app = _unwritable_app(tmp_path)
    with TestClient(app) as c:
        seeded = c.get("/api/groups").json()["groups"]
        assert seeded, "the starter groups should still be there"
        made = c.post("/api/groups", json={"name": "COSMOS Triplet",
                                           "sats": [62902, 62903, 62904],
                                           "reference": 62902})
        assert made.status_code == 201, made.text
        names = [g["name"] for g in c.get("/api/groups").json()["groups"]]
        assert "COSMOS Triplet" in names
        assert len(names) == len(seeded) + 1
    assert not (tmp_path / "groups.json").exists(), "nothing should reach the volume"


def test_a_report_can_be_run_from_a_group_saved_with_no_volume(tmp_path):
    """Save then run, end to end, on the fallback store.

    The recording renderer is the point: it proves the group that only ever
    existed in memory is the one the renderer was asked to draw, not merely
    that some report came back.
    """
    rendered = []
    app = _unwritable_app(tmp_path, rendered)
    try:
        with TestClient(app) as c:
            made = c.post("/api/groups", json={"name": "Run Me",
                                               "sats": [62902, 62903],
                                               "reference": 62902})
            assert made.status_code == 201, made.text
            gid = made.json()["id"]
            started = c.post("/api/runs", json={"groupIds": [gid]})
            assert started.status_code == 202, started.text
            run = _await_run(c, started.json()["id"])
            assert run["status"] == DONE, run
            assert c.get(run["reportUrl"]).status_code == 200
    finally:
        app.state.runner.shutdown()
    assert len(rendered) == 1
    assert rendered[0].group_ids == (gid,)


def test_a_run_over_every_group_includes_one_saved_with_no_volume(tmp_path):
    """The Render report button sends no group ids, so the run has to pick up
    the in-memory group alongside the seeded ones."""
    rendered = []
    app = _unwritable_app(tmp_path, rendered)
    try:
        with TestClient(app) as c:
            gid = c.post("/api/groups", json={"name": "Fallback Group",
                                              "sats": [62902, 62903],
                                              "reference": 62902}).json()["id"]
            started = c.post("/api/runs", json={"days": 7})
            _await_run(c, started.json()["id"])
    finally:
        app.state.runner.shutdown()
    assert gid in rendered[0].group_ids
    assert len(rendered[0].group_ids) == 4


def test_an_unwritable_volume_puts_a_standing_warning_on_the_page(tmp_path):
    """Groups still save, but only into this process, so say so plainly rather
    than letting somebody trust a group that a restart will delete."""
    with TestClient(_unwritable_app(tmp_path)) as c:
        page = c.get("/").text
    assert "kept in memory, not saved" in page
    assert "lost when the pod restarts" in page
    assert "persistent storage add-on" in page
    assert "EROFS" in page
    assert "no storage volume is mounted" in page


def test_a_non_atomic_mount_gets_a_note_on_the_page_not_a_warning(tmp_path,
                                                                  monkeypatch):
    """Saving works on such a mount, so this is a note about a residual risk,
    not the memory warning.

    Asserted at this level as well as in the browser because the note was
    written once and never wired into the page. It rendered nowhere, and only
    the browser test caught it. This makes the wiring itself a unit test.
    """
    monkeypatch.setattr("timeslides.groups.os", _NoPosixOS())
    settings = Settings(udl_user="u", udl_pass="p", storage_path=tmp_path)
    app = create_app(settings=settings, store=GroupStore(settings.groups_file),
                     runner=JobRunner(lambda s, p: "<html/>", settings.runs_path))
    with TestClient(app) as c:
        page = c.get("/").text
    # Match the markup, not the class name: the stylesheet is inlined into the
    # page, so every class name appears in it whether or not anything uses it.
    assert '<p class="note storagenote">' in page
    assert "will survive a restart" in page
    assert "could leave the file truncated" in page
    assert '<div class="err storagewarn"' not in page


def test_the_shell_says_nothing_about_storage_when_it_was_never_probed():
    """The renderer is also used from the command line, where there is no
    volume to probe and nothing to say about one."""
    from timeslides.shell import render_shell

    page = render_shell("OFFICIAL", demo=True, storage=None)
    assert '<p class="note storagenote">' not in page
    assert '<div class="err storagewarn"' not in page


def test_an_atomic_mount_gets_neither_a_note_nor_a_warning(client):
    page = client.get("/").text
    assert '<p class="note storagenote">' not in page
    assert '<div class="err storagewarn"' not in page


def test_the_boot_probe_is_recorded_as_an_audit_event(tmp_path, capsys):
    _unwritable_app(tmp_path)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()
             if line.startswith("{")]
    probes = [line for line in lines if line["msg"] == "boot.storage"]
    assert len(probes) == 1
    assert probes[0]["writable"] is False
    assert probes[0]["mode"] == "memory"
    assert "EROFS" in probes[0]["detail"]
    assert any(line["msg"] == "storage.memory_fallback" for line in lines)
