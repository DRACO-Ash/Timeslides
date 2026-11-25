"""The Node test suite for the browser assets, seen from the Python suite.

tests/js/run.mjs is what the App Store runs (`npm test -- --coverage`) to
measure the JavaScript, which it otherwise counts as wholly uncovered. Two
things here keep it honest:

  * its fixtures are cut from pages the real builder renders, and this
    regenerates them and fails if the committed copies differ, so the Node
    tests can never drift onto a payload the application no longer produces;
  * where Node is installed, the suite is run, so a local `pytest` catches a
    broken JavaScript test before an upload does.

The pipeline's Python job may have no Node. That is a skip with a reason, not
a failure: a failing test stage uploads no artefacts, and the coverage gate
then reads nothing at all.

    TIMESLIDES_REGEN_FIXTURES=1 .venv/bin/python -m pytest tests/test_js_suite.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess

import pytest

from tests.conftest import repo_file

GENERATED = dt.datetime(2026, 11, 16)


def _between(html: str, script_id: str) -> str:
    m = re.search(rf'<script id="{script_id}" type="application/json">(.*?)</script>', html, re.S)
    assert m, f"no {script_id} block in the rendered page"
    return m.group(1)


def _report_data() -> str:
    """Two groups: the post-burn scenario (a rate change and passes), and one
    demo day with both data modes, so switching mode has something to do."""
    from tests.test_lookahead import scenario_panel
    from timeslides.demo import build_demo_modes
    from timeslides.report.builder import build_panel, render_report

    start = dt.datetime(2026, 6, 24)
    end = start + dt.timedelta(days=1)
    group = build_demo_modes(start, end)[0]
    for by_sat in group["objects_by_mode"].values():
        for obj in by_sat.values():
            obj.state_series = {k: v for k, v in obj.state_series.items() if k == "leolabs"}
    demo = build_panel(1, group["name"], group["sat_order"][:3], group["names"],
                       group["objects_by_mode"], ["REAL", "SIM"], group["reference"],
                       False, (start, end), None, first=False)
    html = render_report([scenario_panel(), demo], "OFFICIAL", generated=GENERATED)
    return _between(html, "report-data")


def _shell_data() -> str:
    from timeslides.shell import render_shell
    return _between(render_shell("UNCLASSIFIED", demo=True), "shell-data")


@pytest.mark.parametrize(("name", "build"), [("report-data.json", _report_data),
                                             ("shell-data.json", _shell_data)])
def test_the_node_suite_s_fixtures_are_what_the_application_renders(name, build):
    path = repo_file(f"tests/js/fixtures/{name}", "the Node suite's fixture")
    want = build()
    if os.environ.get("TIMESLIDES_REGEN_FIXTURES") == "1":
        path.write_text(want, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == want, (
        f"{name} no longer matches what the builder renders; regenerate it with "
        "TIMESLIDES_REGEN_FIXTURES=1 and review the diff")


def test_the_node_suite_passes_and_writes_the_coverage_report(tmp_path):
    runner = repo_file("tests/js/run.mjs", "the Node test runner")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed here, so the JavaScript suite NOT run; "
                    "the App Store runs it itself through package.json")
    root = runner.parent.parent.parent
    proc = subprocess.run([node, str(runner), "--coverage"], cwd=root, capture_output=True,
                          text=True, timeout=300, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert " 0 failed" in proc.stdout
    lcov = (root / "coverage" / "lcov.info").read_text(encoding="utf-8")
    expected = ["timeslides/report/assets/report.js", "timeslides/report/assets/picker.js"]
    if (root / "eslint.config.mjs").exists():      # the hostile pass removes it
        expected.append("eslint.config.mjs")
    for rel in expected:
        assert f"SF:{rel}\n" in lcov, f"no coverage record for {rel}"


def test_package_json_declares_every_script_the_platform_runs():
    """1.10.1 failed its App Build stage on `npm error Missing script: "build"`:
    with a package.json present the platform runs `npm run build` as well as
    `npm test -- --coverage`. Both must exist, and neither may need anything
    installed, because the package deliberately has no dependencies."""
    import json
    pkg = json.loads(repo_file("package.json", "the Node test project").read_text())
    assert {"build", "test"} <= set(pkg["scripts"])
    assert not pkg.get("dependencies") and not pkg.get("devDependencies")


def test_the_build_script_passes_here():
    pkg = repo_file("package.json", "the Node test project")
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed here, so `npm run build` NOT run; the "
                    "App Store's App Build stage runs it itself")
    proc = subprocess.run([npm, "run", "build"], cwd=pkg.parent, capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
