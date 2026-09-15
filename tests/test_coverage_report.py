"""The coverage report the gate reads, and the environment it is read in.

The gate has reported "Line coverage: 0.0%" repeatedly against a suite at 100
per cent. What this file does NOT do any more is rewrite the report. An earlier
version normalised its source roots and published copies of it, on the theory
that the scanner could not resolve the paths. The theory was wrong: the gate
once read 79.2% from a report in the plain form pytest-cov writes, and the
coverage configuration was byte-identical when it later read 0.0%. The
configuration was never the cause, and changing a working artefact on an
unproven theory cost four uploads.

What is left here are checks that cost nothing and could not have caused that:
the report measures the whole package, the two exclusion lists agree, and the
helper that decides whether to skip cannot itself throw.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.conftest import (_describe_report, _strip_empty_source_roots,
                           in_git_worktree, repo_file)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """A real coverage report, generated from the real .coveragerc.

    Deliberately not the coverage.xml this suite leaves behind: that file is
    written when the run ends, so a test reading it would be reading the
    previous run's output and would pass on a configuration that had since
    been broken.
    """
    out = tmp_path_factory.mktemp("cov")
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


RAW_SOURCES = """<?xml version="1.0" ?>
<coverage version="7.16.0" line-rate="1">
	<sources>
		<source></source>
		<source>.</source>
	</sources>
	<packages><package><classes>
		<class name="api.py" filename="timeslides/api.py" line-rate="1"/>
	</classes></package></packages>
</coverage>
"""


def test_the_report_is_rooted_at_the_project_not_the_package(report):
    """The defect that produced 0.0%.

    `source = timeslides` roots the report at the package, so it writes an
    absolute path belonging to the machine that ran the tests and names files
    `api.py`. The scanner joins the two and has to land on a file it analysed;
    when the scan runs anywhere else, nothing matches and every analysed file
    is marked uncovered.
    """
    roots = [(s.text or "") for s in report.iter("source")]
    assert not any(r.startswith("/") for r in roots), (
        f"an absolute source root belongs to one machine only: {roots}")
    names = [c.get("filename") for c in report.iter("class")]
    assert names and all(n.startswith("timeslides/") for n in names), (
        f"files must be named from the project root, not the package: {names[:3]}")


def test_the_empty_source_root_is_removed(tmp_path):
    """coverage.py emits an empty <source> ahead of the real one. Joined to a
    filename it gives an absolute path that exists nowhere, so a parser taking
    the first root resolves nothing."""
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    assert "removed 1" in _strip_empty_source_roots(path)
    roots = [(s.text or "") for s in ET.parse(path).getroot().iter("source")]
    assert roots == ["."]


def test_the_strip_keeps_the_coverage_data_intact(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    _strip_empty_source_roots(path)
    root = ET.parse(path).getroot()
    assert root.get("line-rate") == "1"
    assert [c.get("filename") for c in root.iter("class")] == ["timeslides/api.py"]


def test_the_strip_reports_rather_than_raises_on_an_unreadable_report(tmp_path):
    """It runs in a terminal-summary hook, and a hook that raises takes the
    whole run with it, which has already happened once here."""
    path = tmp_path / "coverage.xml"
    path.write_text("not xml at all", encoding="utf-8")
    assert "nothing to normalise" in _strip_empty_source_roots(path)


def test_the_whole_package_is_in_the_denominator(report):
    """An omit that grew would lift the percentage by measuring less.

    Checked against the package on disk rather than a number, so adding a
    module is not a test change, and dropping one from measurement is.
    """
    measured = {c.get("filename") for c in report.iter("class")}
    on_disk = {
        str(p.relative_to(ROOT)) for p in (ROOT / "timeslides").rglob("*.py")
        if "report/assets" not in p.as_posix()
    }
    missing = sorted(on_disk - measured)
    assert missing == [], f"in the package but not measured: {missing}"


def test_the_report_carries_line_level_data(report):
    """A report with no <line> elements imports as nothing, which reads as
    nought per cent rather than as an error."""
    lines = list(report.iter("line"))
    assert lines, "the report has no line-level coverage data at all"
    assert all(line.get("hits") is not None for line in lines)


def test_the_description_helper_summarises_a_report(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="1" lines-covered="1" lines-valid="1">\n'
        "\t<sources><source>.</source></sources>\n"
        "\t<packages><package><classes>\n"
        '\t\t<class name="api.py" filename="timeslides/api.py"/>\n'
        "\t</classes></package></packages>\n"
        "</coverage>\n", encoding="utf-8")
    lines = "\n".join(_describe_report(path))
    assert "timeslides/api.py" in lines
    assert "files in report   1" in lines


def test_the_two_coverage_exclusion_lists_agree():
    """.coveragerc omits a file from the report; sonar-project.properties has
    to exclude the same file from the metric, or the gate counts it as nought
    per cent and fails on a file nobody intended to measure."""
    omitted = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    assert "timeslides/report/assets" in omitted
    sonar = repo_file(
        "sonar-project.properties",
        "If the scanner does not see it either, the coverage exclusions in "
        "it are not being applied; see AUDIT.md.").read_text(encoding="utf-8")
    assert "assets" in sonar


def test_pytest_and_the_coverage_config_name_the_same_source():
    """Two places configuring one thing will disagree. `--cov=<x>` on the
    command line overrides `source` in .coveragerc, so if they ever name
    different things the report's shape depends on how pytest was invoked."""
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    addopts = next(ln for ln in ini.splitlines() if ln.startswith("addopts"))
    assert "--cov" in addopts, "the suite no longer measures coverage"
    if "--cov=" in addopts:
        named = addopts.split("--cov=")[1].split()[0]
        source = (ROOT / ".coveragerc").read_text(encoding="utf-8")
        assert f"source = {named}" in source, (
            f"pytest.ini measures {named}, .coveragerc says otherwise")


# --------------------------------------------------------------------------- #
#  The helper that decides whether to skip must not be able to fail
#
#  A skipif decorator is evaluated at import. An exception there is not one
#  test failing, it is a collection error, and pytest abandons the entire run:
#  684 passing tests never executed, and a test stage that fails uploads no
#  artefacts, so the coverage report never reaches the scan.
# --------------------------------------------------------------------------- #
def test_the_git_probe_returns_false_rather_than_raising_without_git(tmp_path):
    """Run with a PATH that has no git on it, which is the pipeline's image."""
    empty = tmp_path / "nogit"
    empty.mkdir()
    program = tmp_path / "probe.py"
    program.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.conftest import in_git_worktree\n"
        "print(in_git_worktree())\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(program)], cwd=ROOT,
                          env={**os.environ, "PATH": str(empty)},
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"the probe raised:\n{done.stderr}"
    assert done.stdout.strip() == "False"


def test_there_is_only_one_git_probe():
    """The bug was a duplicate, not a typo. The correct implementation already
    existed in tests/conftest.py, with a docstring describing this exact
    failure. A second copy was written next to it without the try/except.

    Found by parsing, not by searching the text: a string search for
    "def in_git_worktree" matches the line of this test that contains it.
    """
    copies = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name.lstrip("_") == "in_git_worktree"):
                copies.append(path.name)
    assert copies == ["conftest.py"], (
        f"the git probe is defined in more than one place: {copies}")


@pytest.mark.skipif(not in_git_worktree(), reason="git is not available here")
def test_the_scanner_configuration_ships():
    """The pipeline's tree not having this file is a property of the pipeline.
    The repository not having it would be a defect."""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "sonar-project.properties"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert tracked.returncode == 0, (
        "sonar-project.properties is not tracked, so it cannot reach the "
        "scanner at all")
