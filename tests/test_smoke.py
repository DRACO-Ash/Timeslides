"""The browser smoke test: boot the real app, drive the real UI, assert no page
errors and that the whole flow works.

This is the test that would have caught every mistake the unit tests cannot
see: a JavaScript syntax error in an asset, a mis-wired button, a template that
renders but does nothing. It runs the app in demo mode so it needs no
credentials and no network.
"""

from __future__ import annotations

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
