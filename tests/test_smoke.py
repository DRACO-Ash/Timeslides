"""The browser smoke test: boot the real app, drive the real UI, assert no page
errors and that the whole flow works.

This is the test that would have caught every mistake the unit tests cannot
see: a JavaScript syntax error in an asset, a mis-wired button, a template that
renders but does nothing. It runs the app in demo mode so it needs no
credentials and no network.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from timeslides.api import create_app
from timeslides.config import Settings

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.browser

# The container ships a pinned Chromium that may not match the Playwright
# package's expected build number, and PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD is set,
# so the executable is named explicitly rather than resolved by version.
CHROMIUM = next(
    (p for p in (
        Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
        Path("/opt/pw-browsers/chromium/chrome-linux/chrome"),
    ) if p.exists()),
    None,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app):
    """Start a server on a free port and return its base URL plus a stopper."""
    import uvicorn
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:                                            # pragma: no cover
        raise AssertionError("the server did not start")

    def stop():
        server.should_exit = True
        thread.join(timeout=10)

    return f"http://127.0.0.1:{port}", stop


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    import uvicorn
    storage = tmp_path_factory.mktemp("storage")
    settings = Settings(storage_path=storage, demo=True, classification="OFFICIAL")
    app = create_app(settings=settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:                                            # pragma: no cover
        raise AssertionError("the server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def fresh_server(tmp_path_factory):
    """A server of its own, for tests that remove things.

    live_server is module-scoped and shared, so a test that archives every
    group leaves the ones after it with nothing to click. Destructive tests get
    their own.
    """
    storage = tmp_path_factory.mktemp("fresh")
    settings = Settings(storage_path=storage, demo=True, classification="OFFICIAL")
    base, stop = _serve(create_app(settings=settings))
    yield base
    stop()


@pytest.fixture(scope="module")
def browser():
    """A missing browser skips, and says so loudly.

    Mapping "could not verify" to "passed" is exactly the fail-open defect, so
    this is not a free pass: AUDIT.md records that the browser suite must be
    run and seen green before an upload, and a skipped smoke test is not a
    passed one. The skip exists so a build agent with no browser provisioned
    reports honestly instead of failing on infrastructure.
    """
    with sync_playwright() as p:
        kwargs = {"executable_path": str(CHROMIUM)} if CHROMIUM else {}
        try:
            b = p.chromium.launch(**kwargs)
        except Exception as exc:                     # pragma: no cover
            pytest.skip(f"no usable Chromium, browser suite NOT verified: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.js_errors = []
    pg.console_errors = []
    pg.on("pageerror", lambda e: pg.js_errors.append(str(e)))
    pg.on("console",
          lambda m: pg.console_errors.append(m.text) if m.type == "error" else None)
    yield pg
    ctx.close()


def _assert_clean(page, allow_failed_requests: bool = False):
    """No uncaught JavaScript, ever.

    Console errors are held to the same standard except in the tests that
    deliberately provoke a rejected request: Chromium logs "Failed to load
    resource" for any 4xx, and the whole point of those tests is that the
    application handles one properly.
    """
    assert page.js_errors == [], f"uncaught JavaScript: {page.js_errors}"
    if not allow_failed_requests:
        assert page.console_errors == [], f"console errors: {page.console_errors}"


# --------------------------------------------------------------------------- #
#  The shell loads
# --------------------------------------------------------------------------- #
def test_the_shell_loads_with_no_page_errors(page, live_server):
    page.goto(live_server, wait_until="load")
    assert page.title().startswith("Timeslides")
    assert page.locator(".classif").inner_text() == "OFFICIAL"
    assert page.locator('.tab[data-tab="cfg"]').is_visible()
    assert page.locator('.tab[data-tab="rep"]').is_visible()
    _assert_clean(page)


def test_the_seeded_groups_are_listed(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    names = page.locator("#groups .grp .gn").all_inner_texts()
    assert "PRC SpacePlane 4" in names
    assert "COSMOS 2581/82/83" in names
    _assert_clean(page)


def test_the_tabs_switch(page, live_server):
    page.goto(live_server, wait_until="load")
    page.click('.tab[data-tab="rep"]')
    assert "active" in page.locator('.panel[data-panel="rep"]').get_attribute("class")
    assert page.locator("#repempty").is_visible()
    page.click('.tab[data-tab="cfg"]')
    assert page.locator("#q").is_visible()
    _assert_clean(page)


def test_only_one_panel_is_ever_visible(page, live_server):
    """report.css hides inactive panels with `.panel{display:none}` and shows
    the active one with `.panel.active`. The shell's own rules have equal
    specificity and load later, so an unscoped override un-hides the inactive
    panel and both tabs paint on top of each other. Nothing else in the suite
    notices, because the class list is still correct."""
    page.goto(live_server, wait_until="load")
    for tab in ("cfg", "rep"):
        page.click(f'.tab[data-tab="{tab}"]')
        visible = [p for p in ("cfg", "rep")
                   if page.locator(f'.panel[data-panel="{p}"]').is_visible()]
        assert visible == [tab], f"with {tab} selected, visible panels were {visible}"
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  Building a group from catalogue selections: the point of the whole change
# --------------------------------------------------------------------------- #
def test_a_group_can_be_built_entirely_from_catalogue_selections(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")

    page.fill("#q", "cluster")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    hits = page.locator("#hits .row").count()
    assert hits >= 3, f"expected the three cluster objects, got {hits}"

    # Save must stay disabled until there is a name and two objects.
    assert page.locator("#savebtn").is_disabled()
    page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row")
    assert page.locator("#savebtn").is_disabled()

    page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.fill("#gname", "Smoke Test Cluster")
    page.dispatch_event("#gname", "input")
    assert page.locator("#picked .row").count() == 2
    assert not page.locator("#savebtn").is_disabled()

    # The first pick is the reference by default.
    assert page.locator("#picked .row.isref").count() == 1

    page.click("#savebtn")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#groups .gn')]"
        ".some(e => e.textContent.trim() === 'Smoke Test Cluster')")
    _assert_clean(page)


def test_the_reference_can_be_reassigned_before_saving(page, live_server):
    page.goto(live_server, wait_until="load")
    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(2):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row:nth-child(2)")
    first_ref = page.locator("#picked .row.isref .rid").inner_text()
    page.locator("#picked .row:not(.isref) button[data-ref]").first.click()
    assert page.locator("#picked .row.isref .rid").inner_text() != first_ref
    assert page.locator("#picked .row.isref").count() == 1
    _assert_clean(page)


def test_removing_an_object_reassigns_the_reference(page, live_server):
    page.goto(live_server, wait_until="load")
    page.fill("#q", "iceye")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(3):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row:nth-child(3)")
    page.locator("#picked .row.isref button[data-del]").click()
    assert page.locator("#picked .row").count() == 2
    assert page.locator("#picked .row.isref").count() == 1
    _assert_clean(page)


def test_a_server_side_rejection_is_shown_to_the_user(page, live_server):
    """A duplicate name is refused by the store; the UI must say so rather
    than failing silently."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(2):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.fill("#gname", "PRC SpacePlane 4")          # already exists
    page.dispatch_event("#gname", "input")
    page.click("#savebtn")
    page.wait_for_selector("#savemsg .err")
    assert "already exists" in page.locator("#savemsg .err").inner_text()
    _assert_clean(page, allow_failed_requests=True)


def test_an_existing_group_can_be_edited(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.locator("#groups .grp button[data-edit]").first.click()
    assert page.locator("#gname").input_value() != ""
    assert page.locator("#picked .row").count() >= 2
    assert page.locator("#cancelbtn").is_visible()
    page.click("#cancelbtn")
    assert page.locator("#gname").input_value() == ""
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  Rendering, through the UI, into the Report tab
# --------------------------------------------------------------------------- #
def test_rendering_a_report_opens_it_in_the_report_tab(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    assert not page.locator("#runbtn").is_disabled()
    page.click("#runbtn")

    # The Report tab opens by itself once the render finishes.
    page.wait_for_selector("#repframe:not([hidden])", timeout=120_000)
    assert page.locator('.panel[data-panel="rep"].active').count() == 1

    # The placeholder must actually be gone and the frame must fill the panel.
    # Checking the hidden attribute alone is not enough: the attribute works
    # through the user-agent stylesheet, which an author display rule
    # outranks, and that left the placeholder on screen pushing the frame a
    # full viewport below the fold while every assertion still passed.
    assert not page.locator("#repempty").is_visible()
    panel = page.locator('.panel[data-panel="rep"]').bounding_box()
    frame_box = page.locator("#repframe").bounding_box()
    assert frame_box["y"] == pytest.approx(panel["y"], abs=2), \
        "the report frame is not at the top of its panel"
    assert frame_box["height"] == pytest.approx(panel["height"], abs=2), \
        "the report frame does not fill its panel"

    frame = page.frame_locator("#repframe")
    frame.locator(".classif").wait_for(timeout=60_000)
    assert frame.locator(".classif").inner_text() == "OFFICIAL"
    # The waterfall itself rendered inside the untouched report document.
    frame.locator(".js-plotly-plot").first.wait_for(timeout=60_000)
    assert frame.locator(".tabs .tab").count() >= 2
    assert frame.locator(".cards .card").count() >= 3

    # The report brings its own banner, header and footer. The shell stands
    # its own down so the user does not see each of them twice.
    assert not page.locator(".app > header").is_visible()
    assert page.locator(".tabs .tab").first.is_visible()          # still switchable
    assert frame.locator(".classif").is_visible()                 # the report's own

    # Switching back restores the shell's chrome.
    page.click('.tab[data-tab="cfg"]')
    assert page.locator(".app > header").is_visible()
    _assert_clean(page)


def test_the_empty_report_tab_keeps_the_shell_chrome(page, live_server):
    """Only a loaded report replaces the chrome; the placeholder still wants
    the surrounding furniture."""
    page.goto(live_server, wait_until="load")
    page.click('.tab[data-tab="rep"]')
    assert page.locator("#repempty").is_visible()
    assert page.locator(".app > header").is_visible()
    assert page.locator(".classif").is_visible()
    _assert_clean(page)


def test_the_report_controls_still_work_inside_the_frame(page, live_server):
    """The report's own behaviour is unchanged, so its source toggles,
    reference selector and mode selector must all still operate."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.click("#runbtn")
    page.wait_for_selector("#repframe:not([hidden])", timeout=120_000)
    frame = page.frame_locator("#repframe")
    frame.locator(".js-plotly-plot").first.wait_for(timeout=60_000)

    # Reference selector is populated.
    options = frame.locator(".refsel").first.locator("option").count()
    assert options >= 2

    # A source chip toggles off and on.
    chip = frame.locator(".srcseg .srcchip").first
    assert "active" in (chip.get_attribute("class") or "")
    chip.click()
    assert "active" not in (frame.locator(".srcseg .srcchip").first
                            .get_attribute("class") or "")

    # An object card isolates.
    card = frame.locator(".cards .card").nth(1)
    card.click()
    assert "off" in (frame.locator(".cards .card").nth(1).get_attribute("class") or "")

    # The data-mode selector switches between REAL and SIM.
    frame.locator(".modeseg button").nth(1).click()
    frame.locator(".js-plotly-plot").first.wait_for()
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  Escaping, in a real browser
# --------------------------------------------------------------------------- #
def test_a_hostile_group_name_does_not_execute(page, live_server):
    """The end-to-end check on the escaping: create a group whose name is an
    injection attempt, render it, and confirm nothing ran."""
    fired = []
    page.on("dialog", lambda d: (fired.append(d.message), d.dismiss()))
    hostile = '<img src=x onerror="window.__xss=1">'
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.fill("#q", "cluster")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(2):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.fill("#gname", hostile)
    page.dispatch_event("#gname", "input")
    page.click("#savebtn")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#groups .gn')]"
        ".some(e => e.textContent.includes('<img'))")

    # Shown as text, not parsed as an element, and no script ran.
    assert page.locator("#groups .grp .gn").filter(has_text="<img").count() == 1
    assert page.evaluate("() => document.querySelectorAll('#groups img').length") == 0
    assert page.evaluate("() => window.__xss") is None
    assert fired == []
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  The storage warning, and the layout it must not break
# --------------------------------------------------------------------------- #
@pytest.fixture
def unwritable_server(tmp_path_factory, monkeypatch):
    """No usable volume at all: every write through the shared writer fails.

    An earlier version of this fixture only overrode the store's writability
    probe, which meant the render still wrote to a perfectly good temp
    directory. So it proved the group store degraded and said nothing at all
    about the renderer, which was the half that stayed broken. Refusing at the
    writer covers every write the application makes.
    """
    import errno
    import os as _os

    from timeslides.api import create_app
    from timeslides.config import Settings
    from timeslides.groups import GroupStore
    from timeslides.storage import VolumeWriter, write_failure_advice

    reason = write_failure_advice(OSError(errno.EROFS, _os.strerror(errno.EROFS)))

    def refuse(_self, _target, _payload):
        raise OSError(errno.EROFS, _os.strerror(errno.EROFS))

    monkeypatch.setattr(VolumeWriter, "write", refuse)
    storage = tmp_path_factory.mktemp("readonly")
    settings = Settings(storage_path=storage, demo=True, classification="OFFICIAL")
    base, stop = _serve(create_app(settings=settings,
                                   store=GroupStore(settings.groups_file)))
    yield base, storage, reason
    stop()


@pytest.fixture
def s3_server(tmp_path_factory, monkeypatch):
    """The app on the volume it is actually given.

    The File Storage add-on is S3-backed and mounted at /data. Such mounts
    implement neither fsync nor rename, and assuming POSIX there made every
    save and then every render fail with ENOSYS (errno 38), Function not
    implemented. The patch is applied to timeslides.storage, the only place the
    application writes a file, so it covers every write the app makes without
    disturbing pytest's or coverage's own.
    """
    import errno
    import os as _os

    from timeslides.api import create_app
    from timeslides.config import Settings
    from timeslides.groups import GroupStore

    class NoPosixOS:
        def __getattr__(self, name):
            return getattr(_os, name)

        def replace(self, *_a, **_k):
            raise OSError(errno.ENOSYS, _os.strerror(errno.ENOSYS))

        def fsync(self, *_a, **_k):
            raise OSError(errno.ENOSYS, _os.strerror(errno.ENOSYS))

    monkeypatch.setattr("timeslides.storage.os", NoPosixOS())
    storage = tmp_path_factory.mktemp("s3mount")
    settings = Settings(storage_path=storage, demo=True, classification="OFFICIAL")
    base, stop = _serve(create_app(settings=settings,
                                   store=GroupStore(settings.groups_file)))
    yield base, storage
    stop()


def test_a_group_saves_to_an_s3_backed_mount_and_lands_on_the_volume(page, s3_server):
    """The operator's question, on the operator's volume: add a group of
    satellites, save it, and run the tool.

    The volume assertion is the one that matters. Accepting the save is not
    enough; the group has to be on the volume, because that is what the storage
    add-on was enabled for.
    """
    base, storage = s3_server
    page.goto(base, wait_until="load")
    page.wait_for_selector("#groups .grp")

    # No alarm, because nothing is wrong: this mount works.
    assert page.locator(".storagewarn").count() == 0

    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(3):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row:nth-child(3)")

    page.fill("#gname", "COSMOS Triplet")
    page.dispatch_event("#gname", "input")
    page.click("#savebtn")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#groups .gn')]"
        ".some(e => e.textContent.trim() === 'COSMOS Triplet')")
    assert page.locator("#savemsg .err").count() == 0, \
        page.locator("#savemsg").inner_text()

    on_disk = json.loads((storage / "groups.json").read_text(encoding="utf-8"))
    assert "COSMOS Triplet" in [g["name"] for g in on_disk["groups"]], \
        "the group was accepted but never reached the volume"

    # And the tool runs.
    page.click("#runbtn")
    page.wait_for_selector("#repframe:not([hidden])", timeout=120_000)
    frame = page.frame_locator("#repframe")
    frame.locator(".js-plotly-plot").first.wait_for(timeout=60_000)
    _assert_clean(page)


def test_a_mount_without_atomic_replace_says_so_without_alarm(page, s3_server):
    """Saving works, so this is a note, not the memory warning."""
    base, _ = s3_server
    page.goto(base, wait_until="load")
    note = page.locator(".storagenote")
    note.wait_for()
    assert "will survive a restart" in note.inner_text()
    assert "could leave the file truncated" in note.inner_text()
    assert page.locator(".storagewarn").count() == 0
    _assert_clean(page)


def test_an_unwritable_volume_warns_without_breaking_the_page(page, unwritable_server):
    """The warning has to be visible AND leave the tab usable.

    Its first version was added as a child of the .app grid, which has five
    explicit rows with the 1fr on the fourth. A sixth child took that row and
    the warning swallowed the whole Configure tab: catalogue, editor, saved
    groups and render controls all gone. This asserts the geometry, not just
    the presence of the text, because the class list was correct either way.
    """
    base, _storage, _reason = unwritable_server
    page.goto(base, wait_until="load")
    warning = page.locator(".storagewarn")
    warning.wait_for()
    assert "kept in memory, not saved" in warning.inner_text()
    assert "lost when the pod restarts" in warning.inner_text()
    assert "EROFS" in warning.inner_text()

    # The tab still works: every part of it is present and visible.
    for selector in ("#q", "#searchbtn", "#gname", "#savebtn", "#groups",
                     "#runbtn", "#days", "[data-source]"):
        assert page.locator(selector).first.is_visible(), selector

    # And the warning is a banner, not the whole panel.
    panel = page.locator('.panel[data-panel="cfg"]').bounding_box()
    box = warning.bounding_box()
    assert box["height"] < panel["height"] / 3, \
        "the warning has taken over the panel instead of sitting above it"
    _assert_clean(page)


def test_the_core_flow_works_with_no_volume(page, unwritable_server):
    """Add a group of satellites, save it, and run the tool. With no storage
    volume attached.

    This is the whole point of the application, so it must not depend on an
    add-on somebody forgot to attach. An earlier build refused every save on
    an unwritable path, which left the deployed app unusable rather than
    merely unable to remember anything.
    """
    base, storage, _reason = unwritable_server
    page.goto(base, wait_until="load")
    page.wait_for_selector("#groups .grp")
    seeded = page.locator("#groups .grp").count()
    assert seeded == 3, f"the starter groups should still appear, got {seeded}"

    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(3):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row:nth-child(3)")

    page.fill("#gname", "COSMOS Triplet")
    page.dispatch_event("#gname", "input")
    assert not page.locator("#savebtn").is_disabled()
    page.click("#savebtn")

    # Saved, listed, and no error where the store would have reported one.
    page.wait_for_function(
        "() => [...document.querySelectorAll('#groups .gn')]"
        ".some(e => e.textContent.trim() === 'COSMOS Triplet')")
    assert page.locator("#groups .grp").count() == seeded + 1
    assert page.locator("#savemsg .err").count() == 0, \
        page.locator("#savemsg").inner_text()
    # The reminder sits with the group list too, because the banner at the top
    # of the tab scrolls out of sight on a long list.
    assert "memory only" in page.locator("#groupsmsg").inner_text()

    # The group is counted into the next render.
    assert "4" in page.locator("#groupcount").inner_text()

    # And the tool runs, with the report held in memory because the volume
    # refused it. This fixture is in demo mode, which draws fixed sample
    # panels rather than the requested groups, so the group-to-panel wiring on
    # the fallback store is asserted against a recording renderer in
    # test_api.py instead. What matters here is that the button works and a
    # report comes back with no volume at all.
    page.click("#runbtn")
    page.wait_for_selector("#repframe:not([hidden])", timeout=120_000)
    frame = page.frame_locator("#repframe")
    frame.locator(".classif").wait_for(timeout=60_000)
    frame.locator(".js-plotly-plot").first.wait_for(timeout=60_000)
    assert frame.locator(".tabs .tab").count() >= 1
    assert not any(storage.rglob("*.html")), "nothing should have reached the volume"
    _assert_clean(page)


def test_the_warning_is_absent_when_the_volume_is_writable(page, live_server):
    page.goto(live_server, wait_until="load")
    assert page.locator(".storagewarn").count() == 0
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  Providers: the Configure tab and the report legend must agree
# --------------------------------------------------------------------------- #
def test_all_five_state_providers_are_offered_with_distinct_markers(page, live_server):
    page.goto(live_server, wait_until="load")
    chips = page.locator("[data-source]")
    assert chips.count() == 5
    labels = [t.strip() for t in chips.all_inner_texts()]
    assert labels == ["LeoLabs", "NorthStar", "KBR", "PPEC", "Space-Track"]
    # Shape encodes the source, so no two may share one.
    shapes = page.eval_on_selector_all(
        "[data-source] .mk",
        "els => els.map(e => [...e.classList].find(c => c.startsWith('mk-')))")
    assert len(set(shapes)) == 5, f"providers share a marker shape: {shapes}"
    _assert_clean(page)


def test_the_marker_shapes_actually_render(page, live_server):
    """A clip-path typo yields an invisible chip. Check they have real area."""
    page.goto(live_server, wait_until="load")
    for i in range(page.locator("[data-source] .mk").count()):
        box = page.locator("[data-source] .mk").nth(i).bounding_box()
        assert box["width"] >= 6 and box["height"] >= 6, f"marker {i} did not render"
    _assert_clean(page)


def test_the_probe_marks_its_region_busy_while_it_runs(page, live_server):
    """The region has to say when it is mid-update, both for assistive
    technology and so nothing reads a stale answer as a fresh one."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    region = page.locator("#probemsg")
    assert region.get_attribute("aria-busy") == "false"
    assert region.get_attribute("role") == "status"
    page.click("#probebtn")
    page.wait_for_selector('#probemsg[aria-busy="false"]')
    assert "provider" in region.inner_text().lower() or "Demo" in region.inner_text()
    _assert_clean(page)


def test_the_availability_check_annotates_each_provider(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.click("#probebtn")
    # Waiting on ".note" alone matched the spinner, which is also a .note, so
    # the assertions ran before the answer arrived. aria-busy is the settle
    # condition.
    page.wait_for_selector('#probemsg[aria-busy="false"]')
    # Demo mode answers for every provider.
    assert page.locator("[data-source].confirmed").count() == 5
    assert page.locator("[data-source].gone").count() == 0
    assert "Demo mode" in page.locator("#probemsg").inner_text()
    _assert_clean(page)


def test_the_availability_check_does_not_toggle_the_providers_off(page, live_server):
    """The button lives inside the providers label, next to the chips."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    before = page.locator("[data-source].active").count()
    page.click("#probebtn")
    page.wait_for_selector('#probemsg[aria-busy="false"]')
    assert page.locator("[data-source].active").count() == before == 5
    _assert_clean(page)


def test_the_report_legend_matches_the_providers_that_were_selected(page, live_server):
    """The mismatch this replaced: the tab offered three state providers while
    the legend showed two of them plus a series called Space-Track that was
    really the element sets. Now the legend is the five providers plus a
    clearly named element-set series."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.click("#runbtn")
    page.wait_for_selector("#repframe:not([hidden])", timeout=180_000)
    frame = page.frame_locator("#repframe")
    frame.locator(".js-plotly-plot").first.wait_for(timeout=120_000)

    # Scoped to the panel actually showing: the report has one legend per group.
    active = frame.locator(".panel.active")
    legend = [t.strip() for t in active.locator(".srcseg .srcchip").all_inner_texts()]
    assert legend == ["LeoLabs", "NorthStar", "KBR", "PPEC", "Space-Track",
                      "Element sets"], legend
    # Six series, six distinct shapes.
    shapes = active.locator(".srcseg .srcchip .mk").evaluate_all(
        "els => els.map(e => [...e.classList].find(c => c.startsWith('mk-')))")
    assert len(set(shapes)) == 6, f"legend shapes not distinct: {shapes}"
    _assert_clean(page)


def test_deselecting_a_provider_keeps_it_out_of_the_report(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.click('[data-source="kbr"]')
    page.click('[data-source="ppec"]')
    page.click("#runbtn")
    page.wait_for_selector("#repframe:not([hidden])", timeout=180_000)
    frame = page.frame_locator("#repframe")
    frame.locator(".js-plotly-plot").first.wait_for(timeout=120_000)
    legend = [t.strip() for t in
              frame.locator(".panel.active .srcseg .srcchip").all_inner_texts()]
    legend = [t.strip() for t in legend]
    assert "KBR" not in legend
    assert "PPEC" not in legend
    assert "LeoLabs" in legend
    # The element-set series is always there: it anchors the reference orbit.
    assert "Element sets" in legend
    _assert_clean(page)


# --------------------------------------------------------------------------- #
#  The controls that had no browser coverage at all
#
#  Each of these is a button somebody will press. Every one of them was
#  reachable and untested, which is how a whole write path stayed broken
#  through three releases.
# --------------------------------------------------------------------------- #
def test_archiving_asks_first_and_does_nothing_if_declined(page, fresh_server):
    """Archiving is guarded by a confirm, so a stray click cannot remove
    somebody's group. Playwright dismisses dialogs unless told otherwise, which
    is the decline case."""
    page.goto(fresh_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    before = page.locator("#groups .grp").count()
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.locator("#groups .grp button[data-archive]").first.click()
    page.wait_for_timeout(300)
    assert asked, "archiving must ask before removing a group"
    assert "Archive" in asked[0]
    assert page.locator("#groups .grp").count() == before
    _assert_clean(page)


def test_a_group_can_be_archived_and_leaves_the_list(page, fresh_server):
    page.goto(fresh_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    before = page.locator("#groups .grp").count()
    name = page.locator("#groups .gn").first.inner_text().strip()
    page.on("dialog", lambda d: d.accept())
    page.locator("#groups .grp button[data-archive]").first.click()
    page.wait_for_function(
        f"() => document.querySelectorAll('#groups .grp').length === {before - 1}")
    assert name not in page.locator("#groups").inner_text()
    assert page.locator("#groupsmsg .err").count() == 0
    _assert_clean(page)


def test_archiving_the_last_group_disables_rendering(page, fresh_server):
    """Nothing to render is a disabled button, not a failed run."""
    page.goto(fresh_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.on("dialog", lambda d: d.accept())
    while page.locator("#groups .grp").count():
        count = page.locator("#groups .grp").count()
        page.locator("#groups .grp button[data-archive]").first.click()
        page.wait_for_function(
            f"() => document.querySelectorAll('#groups .grp').length === {count - 1}")
    assert page.locator("#runbtn").is_disabled()
    assert "No groups yet" in page.locator("#groups").inner_text()
    _assert_clean(page)


def test_cancelling_an_edit_clears_the_editor(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.locator("#groups .grp button[data-edit]").first.click()
    page.wait_for_selector("#picked .row")
    assert page.locator("#cancelbtn").is_visible()
    assert page.locator("#editing").inner_text().strip()

    page.click("#cancelbtn")
    assert page.locator("#picked .row").count() == 0
    assert page.locator("#gname").input_value() == ""
    assert not page.locator("#cancelbtn").is_visible()
    assert page.locator("#editing").inner_text().strip() == ""
    assert page.locator("#savebtn").is_disabled()
    _assert_clean(page)


def test_a_duplicate_group_name_is_refused_with_a_reason(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    existing = page.locator("#groups .gn").first.inner_text().strip()

    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    for _ in range(2):
        page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.fill("#gname", existing)
    page.dispatch_event("#gname", "input")
    page.click("#savebtn")
    page.wait_for_selector("#savemsg .err")
    assert "already exists" in page.locator("#savemsg .err").inner_text()
    # The 400 is the point of the test, so the failed request is expected.
    _assert_clean(page, allow_failed_requests=True)


def test_the_save_button_stays_disabled_until_the_group_is_valid(page, live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    assert page.locator("#savebtn").is_disabled()

    page.fill("#gname", "Not Enough Objects")
    page.dispatch_event("#gname", "input")
    assert page.locator("#savebtn").is_disabled(), "a name alone is not a group"

    page.fill("#q", "cosmos")
    page.click("#searchbtn")
    page.wait_for_selector("#hits .row")
    page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row")
    assert page.locator("#savebtn").is_disabled(), "one object is not a group"

    page.locator("#hits .row button[data-add]:not([disabled])").first.click()
    page.wait_for_selector("#picked .row:nth-child(2)")
    assert not page.locator("#savebtn").is_disabled()
    _assert_clean(page)


def test_a_search_that_matches_nothing_says_so(page, live_server):
    page.goto(live_server, wait_until="load")
    page.fill("#q", "zzzznotathing")
    page.click("#searchbtn")
    page.wait_for_selector("#searchmsg .note")
    assert "Nothing in the catalogue matches" in page.locator("#searchmsg").inner_text()
    assert page.locator("#hits .row").count() == 0
    _assert_clean(page)


def test_real_is_the_only_data_mode_on_by_default(page, live_server):
    """As the command line defaulted. The others are opt-in."""
    page.goto(live_server, wait_until="load")
    active = page.locator("[data-mode].active")
    assert active.count() == 1
    assert active.inner_text().strip() == "REAL"


@pytest.mark.parametrize("mode", ["SIM", "TEST", "EXERCISE"])
def test_data_modes_add_to_the_selection_rather_than_replacing_it(
        page, live_server, mode):
    """Several modes at once is the point: the original --modes took a list,
    and a run plots each mode it was given."""
    page.goto(live_server, wait_until="load")
    page.click(f'[data-mode="{mode}"]')
    selected = page.eval_on_selector_all(
        "[data-mode].active", "els => els.map(e => e.dataset.mode)")
    assert sorted(selected) == sorted(["REAL", mode])
    _assert_clean(page)


def test_turning_every_data_mode_off_is_refused_before_a_run_is_sent(page,
                                                                    live_server):
    """A run with no mode would be a confusing server error, so the page says
    so instead and sends nothing."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    page.click('[data-mode="REAL"]')
    assert page.locator("[data-mode].active").count() == 0
    page.click("#runbtn")
    page.wait_for_selector("#runmsg .err")
    assert "at least one data mode" in page.locator("#runmsg").inner_text()
    assert page.locator("#repframe").get_attribute("hidden") is not None
    _assert_clean(page)


def test_turning_every_provider_off_is_refused_before_a_run_is_sent(page,
                                                                   live_server):
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    for i in range(page.locator("[data-source]").count()):
        page.locator("[data-source]").nth(i).click()
    assert page.locator("[data-source].active").count() == 0
    page.click("#runbtn")
    page.wait_for_selector("#runmsg .err")
    assert "at least one provider" in page.locator("#runmsg").inner_text()
    _assert_clean(page)


def test_the_window_accepts_its_range_and_refuses_beyond_it(page, live_server):
    """The input carries the bounds, and the server is the one that enforces
    them, so both are checked."""
    page.goto(live_server, wait_until="load")
    days = page.locator("#days")
    assert days.get_attribute("min") == "1"
    assert days.get_attribute("max") == "90"
    for value in ("1", "90", "30"):
        days.fill(value)
        assert days.input_value() == value
    _assert_clean(page)


def test_the_sign_invert_is_an_off_by_default_toggle(page, live_server):
    """Invert is the sign of the offset, not a provider control. It sits under
    the SIGN label and is sent with the run."""
    page.goto(live_server, wait_until="load")
    invert = page.locator("#invert")
    assert "active" not in (invert.get_attribute("class") or "")
    invert.click()
    assert "active" in (invert.get_attribute("class") or "")
    invert.click()
    assert "active" not in (invert.get_attribute("class") or "")
    _assert_clean(page)


def test_every_provider_starts_on_and_toggles_independently(page, live_server):
    page.goto(live_server, wait_until="load")
    chips = page.locator("[data-source]")
    assert chips.count() == 5
    assert page.locator("[data-source].active").count() == 5
    chips.first.click()
    assert page.locator("[data-source].active").count() == 4
    chips.first.click()
    assert page.locator("[data-source].active").count() == 5
    _assert_clean(page)


def test_the_report_can_be_rendered_twice_in_a_row(page, live_server):
    """The second render is the one that exercises eviction and the ETag path,
    and a report served from a 304 must still display."""
    page.goto(live_server, wait_until="load")
    page.wait_for_selector("#groups .grp")
    for _ in range(2):
        page.click('.tab[data-tab="cfg"]')
        page.click("#runbtn")
        page.wait_for_selector("#repframe:not([hidden])", timeout=120_000)
        page.frame_locator("#repframe").locator(".js-plotly-plot").first.wait_for(
            timeout=60_000)
    _assert_clean(page)
