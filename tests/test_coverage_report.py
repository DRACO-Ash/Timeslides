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

import ast
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from fnmatch import fnmatch
from pathlib import Path

import pytest

from tests.conftest import (DEFAULT_REPORT_COPY, _describe_report,
                           _normalise_source_roots, in_git_worktree)

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


def test_the_report_omits_what_cannot_be_measured_honestly():
    """Two files that cannot be measured in process, and the reason for each.

    Kept separate from the SonarQube half below, because .coveragerc is always
    in the tree the tests run in and sonar-project.properties is not.
    """
    omitted = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    for path in ("app.py", "timeslides/report/assets/"):
        assert path in omitted, f"{path} is not omitted from the report"


def test_the_two_coverage_exclusion_lists_agree():
    """.coveragerc omits a file from the report; sonar-project.properties has
    to exclude the same file from the metric, or the gate counts it as nought
    per cent and fails on a file nobody intended to measure.

    Skipped rather than failed when the scanner's configuration is not in the
    tree. The App Store's test stage runs against the unpacked upload and
    sonar-project.properties is not in it, which failed this test in the
    pipeline while every assertion it makes was true of the repository. A test
    that asserts a file exists in an environment that legitimately does not
    have it is testing the environment, not the code.

    The skip cannot hide a deleted file: the companion test below fails if it
    is missing from a checkout that does have git, which is every checkout a
    person or this repository's own CI works in.
    """
    sonar_config = ROOT / "sonar-project.properties"
    if not sonar_config.exists():
        pytest.skip(
            "sonar-project.properties is not in this tree. The App Store's "
            "test stage runs against the unpacked upload, which does not "
            "carry it. If the scanner does not see it either, the coverage "
            "exclusions in it are not being applied; see AUDIT.md.")
    sonar = sonar_config.read_text(encoding="utf-8")
    assert "app.py" in sonar
    assert "assets" in sonar


@pytest.mark.skipif(not in_git_worktree(), reason="git is not available here")
def test_the_scanner_configuration_ships():
    """The other half of the skip above.

    The pipeline's tree not having this file is a property of the pipeline. The
    repository not having it would be a defect, and without this check the skip
    above would swallow it silently.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "sonar-project.properties"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert tracked.returncode == 0, (
        "sonar-project.properties is not tracked, so it cannot reach the "
        "scanner at all")


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


# --------------------------------------------------------------------------- #
#  The helper that decides whether to skip must not be able to fail
#
#  A skipif decorator is evaluated at import. An exception there is not one
#  test failing, it is a collection error, and pytest abandons the entire run:
#  "Interrupted: 1 error during collection", 684 passing tests never executed.
#  That is what a second, private copy of this helper without a try/except did
#  in the pipeline, where .git exists and the git binary does not.
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
    assert done.returncode == 0, f"the probe raised instead of returning:\n{done.stderr}"
    assert done.stdout.strip() == "False"


def test_there_is_only_one_git_probe():
    """The bug was a duplicate, not a typo.

    The correct implementation already existed in tests/conftest.py, with a
    docstring describing this exact failure. A second copy was written next to
    it without the try/except. Two implementations of one decision will
    diverge, and the one that diverges is the one nobody is looking at.

    Found by parsing, not by searching the text: a string search for
    "def in_git_worktree" matches the line of this test that contains it, which
    is the fourth time in this repository that a check has reported on its own
    source. The rule is in CODE-QUALITY.md; this is the rule being followed.
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


# --------------------------------------------------------------------------- #
#  The empty source root
#
#  The second 0.0%. coverage.py writes two source roots for a report rooted at
#  the project, and the first is empty. A scanner resolves an entry by joining
#  a root to a filename, and an empty root turns "timeslides/api.py" into the
#  absolute "/timeslides/api.py", which exists nowhere. A parser that takes the
#  first root rather than trying each resolves nothing, and a report in which
#  nothing resolves reads as nought per cent rather than as no data.
# --------------------------------------------------------------------------- #
RAW_SOURCES = """<?xml version="1.0" ?>
<coverage version="7.16.0" line-rate="1">
	<sources>
		<source></source>
		<source>.</source>
	</sources>
	<packages>
		<classes>
			<class name="api.py" filename="timeslides/api.py" line-rate="1"/>
		</classes>
	</packages>
</coverage>
"""


def test_coverage_py_really_does_emit_an_empty_source_root(report):
    """The premise, checked against the tool rather than assumed.

    This fixture is generated by coverage.py directly, without the conftest
    hook that cleans it up, so it shows the raw output. If a future coverage.py
    stops emitting the empty element, this test says so and the normaliser
    becomes redundant rather than silently pointless.
    """
    roots = [(s.text or "") for s in report.iter("source")]
    assert "" in roots, (
        "coverage.py no longer emits an empty source root; the normaliser in "
        "conftest.py may no longer be needed")


def test_an_empty_source_root_resolves_to_an_absolute_path():
    """Why the empty element matters, stated as arithmetic rather than opinion."""
    assert "" + "/" + "timeslides/api.py" == "/timeslides/api.py"
    assert "." + "/" + "timeslides/api.py" == "./timeslides/api.py"


def test_the_normaliser_removes_the_empty_root(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    message = _normalise_source_roots(path)
    assert "removed 1" in message, message
    roots = [(s.text or "") for s in ET.parse(path).getroot().iter("source")]
    assert roots == ["."]


def test_the_normaliser_keeps_the_coverage_data_intact(tmp_path):
    """It drops an ambiguous element. It must not touch a number."""
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    _normalise_source_roots(path)
    root = ET.parse(path).getroot()
    assert root.get("line-rate") == "1"
    assert [c.get("filename") for c in root.iter("class")] == ["timeslides/api.py"]


def test_the_normaliser_is_idempotent(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    _normalise_source_roots(path)
    once = path.read_text(encoding="utf-8")
    second = _normalise_source_roots(path)
    assert "already unambiguous" in second, second
    assert path.read_text(encoding="utf-8") == once


def test_the_normaliser_leaves_at_least_one_root(tmp_path):
    """A report with nothing but empty roots must not end up with none, which
    would be a different kind of unresolvable."""
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES.replace("<source>.</source>", "<source></source>"),
                    encoding="utf-8")
    _normalise_source_roots(path)
    roots = [(s.text or "") for s in ET.parse(path).getroot().iter("source")]
    assert roots == ["."]


def test_the_normaliser_reports_rather_than_raises_on_a_report_it_cannot_read(
        tmp_path):
    """It runs in a terminal-summary hook. A hook that raises takes the run
    with it, which has already happened once here."""
    path = tmp_path / "coverage.xml"
    path.write_text("not xml at all", encoding="utf-8")
    assert "nothing to normalise" in _normalise_source_roots(path)


def test_the_description_helper_summarises_a_report(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(RAW_SOURCES, encoding="utf-8")
    lines = "\n".join(_describe_report(path))
    assert "timeslides/api.py" in lines
    assert "files in report   1" in lines


def test_the_report_this_suite_leaves_behind_is_unambiguous():
    """The end-to-end check: what the scanner would actually read.

    Written by the previous run of this suite, through the conftest hook, which
    is exactly the path the pipeline takes.
    """
    produced = ROOT / "coverage.xml"
    if not produced.exists():
        pytest.skip("no coverage.xml from a previous run in this tree")
    root = ET.parse(produced).getroot()
    roots = [(s.text or "") for s in root.iter("source")]
    assert "" not in roots, f"an empty source root survived: {roots}"
    assert not any(r.startswith("/") for r in roots), roots
    names = [c.get("filename") for c in root.iter("class")]
    assert names and all(n.startswith("timeslides/") for n in names)


# --------------------------------------------------------------------------- #
#  The second copy, at the path the plugin searches by default
# --------------------------------------------------------------------------- #
def test_the_conventional_copy_matches_the_plugin_s_default_pattern():
    """SonarQube's Python plugin defaults sonar.python.coverage.reportPaths to
    coverage-reports/*coverage-*.xml. A report written there is found with no
    configuration, which matters because the App Store's tree does not carry
    our sonar-project.properties."""
    assert fnmatch(DEFAULT_REPORT_COPY, "coverage-reports/*coverage-*.xml"), (
        f"{DEFAULT_REPORT_COPY} would not be found by the default pattern")


def test_the_conventional_copy_is_the_same_report():
    """A second copy that differs from the first is two answers to one
    question. It is copied after normalisation, so it carries the same
    unambiguous source root."""
    produced = ROOT / "coverage.xml"
    copy = ROOT / DEFAULT_REPORT_COPY
    if not (produced.exists() and copy.exists()):
        pytest.skip("no coverage report from a previous run in this tree")
    assert copy.read_bytes() == produced.read_bytes()
    roots = [(s.text or "") for s in ET.parse(copy).getroot().iter("source")]
    assert "" not in roots, roots


@pytest.mark.skipif(not in_git_worktree(), reason="git is not available here")
def test_the_conventional_copy_is_not_committed():
    """It is generated on every run. A generated file in the repository is a
    file that goes stale and then lies."""
    ignored = subprocess.run(
        ["git", "check-ignore", DEFAULT_REPORT_COPY],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert ignored.returncode == 0, (
        f"{DEFAULT_REPORT_COPY} is not git-ignored and would be committed")
