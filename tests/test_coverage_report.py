"""The coverage report has to be readable by the scanner, not just correct.

The quality gate reported "Line coverage: 0.0% (required: 80%)" on a suite that
was at 100 per cent locally, and the pipeline's own advice was to write more
tests. There was nothing wrong with the tests. The report named every file
relative to an absolute path on the machine that produced it, so the scanner
resolved none of them and counted every analysed line as uncovered.

That failure is invisible from inside the suite: the number the runner prints
is right, the file is written, and the only symptom is a gate result a pipeline
run away. So the shape of the report is asserted here, by generating one the
same way the pipeline does and reading it back.

What matters, in order:

  1. no absolute path anywhere in the report, because a path from the runner is
     meaningless on the scanner;
  2. filenames relative to the project root, which is what SonarQube joins a
     source root to;
  3. the whole package in the denominator, so an `omit` cannot quietly shrink
     what is being measured and lift the percentage.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """A real coverage report, generated from the real .coveragerc.

    Deliberately not the coverage.xml this suite happens to leave behind: that
    file is written when the run ends, so a test reading it would be reading
    the previous run's output and would pass on a configuration that had since
    been broken.

    A one-line program is enough. The shape of the report is set by the
    configuration, not by how much of the application the program touches.
    """
    out = tmp_path_factory.mktemp("cov")
    # The program lives outside the project root, so it is the thing being run
    # rather than a thing being measured.
    program = out / "touch_the_package.py"
    program.write_text("import timeslides.models\n", encoding="utf-8")
    # PYTHONPATH rather than cwd: running a script puts the script's own
    # directory on sys.path, not the working directory.
    env = {**os.environ, "COVERAGE_FILE": str(out / ".coverage"),
           "PYTHONPATH": str(ROOT)}
    for argv in ([sys.executable, "-m", "coverage", "run", str(program)],
                 [sys.executable, "-m", "coverage", "xml",
                  "-o", str(out / "coverage.xml")]):
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                              text=True, check=False)
        assert done.returncode == 0, done.stderr
    return ET.parse(out / "coverage.xml").getroot()


def _filenames(report) -> list:
    return [c.get("filename") for c in report.iter("class")]


def test_no_source_root_is_an_absolute_path(report):
    """The defect itself.

    `source = timeslides` made coverage.py write the package's absolute path as
    the report root and name files relative to it. On the scanner that
    directory does not exist, so nothing resolved.
    """
    roots = [(s.text or "") for s in report.iter("source")]
    absolute = [r for r in roots if r.startswith("/") or ":" in r]
    assert absolute == [], (
        "a source root from the machine that ran the tests is meaningless on "
        f"the scanner: {absolute}")


def test_every_file_is_named_from_the_project_root(report):
    """SonarQube joins a source root to a filename. With the root relative,
    the filename has to carry the package directory or the join lands in the
    wrong place."""
    names = _filenames(report)
    assert names, "the report contains no files at all"
    wrong = [n for n in names if not n.startswith("timeslides/")]
    assert wrong == [], f"not relative to the project root: {wrong}"
    assert not any(n.startswith("/") for n in names)


def test_the_whole_package_is_in_the_denominator(report):
    """An omit that grew would lift the percentage by measuring less.

    Checked against the package on disk rather than a number, so adding a
    module is not a test change, and dropping one from measurement is.
    """
    measured = set(_filenames(report))
    on_disk = {
        str(p.relative_to(ROOT)) for p in (ROOT / "timeslides").rglob("*.py")
        if "report/assets" not in p.as_posix()
    }
    missing = sorted(on_disk - measured)
    assert missing == [], f"in the package but not measured: {missing}"


def test_the_configuration_says_so_rather_than_relying_on_a_default(report):
    """relative_files defaults to off, and the report above is the only thing
    that would notice if it were removed. Pinned here as well so the reason
    survives with the setting."""
    text = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    assert "relative_files = True" in text
    assert "source = ." in text


def test_the_two_coverage_exclusion_lists_agree():
    """.coveragerc omits a file from the report; sonar-project.properties has
    to exclude the same file from the metric, or SonarQube counts it as nought
    per cent and the gate fails on a file nobody intended to measure."""
    omitted = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    sonar = (ROOT / "sonar-project.properties").read_text(encoding="utf-8")
    for path in ("app.py", "timeslides/report/assets/"):
        assert path in omitted, f"{path} is not omitted from the report"
    assert "app.py" in sonar
    assert "assets" in sonar


def test_pytest_does_not_override_the_coverage_source():
    """One source of truth for what the report is rooted at.

    `--cov=timeslides` on the command line beats `source` in .coveragerc, and
    the two then disagree: the config roots the report at the project and names
    files `timeslides/api.py`, the command line roots it at the package and
    names them `api.py`. Which one the scanner reads depends on how the
    pipeline happens to invoke pytest, and the symptom is a coverage figure,
    not an error.
    """
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    addopts = next(ln for ln in ini.splitlines() if ln.startswith("addopts"))
    assert "--cov " in f"{addopts} ", "the suite no longer measures coverage"
    assert "--cov=" not in addopts, (
        "a --cov with an argument overrides .coveragerc and changes the shape "
        f"of the report the scanner reads: {addopts}")
