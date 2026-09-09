"""The job runner: single-flight, failure containment, bounded growth."""

from __future__ import annotations

import threading

import pytest

from timeslides.errors import ComputeError, NotFoundError
from timeslides.jobs import DONE, FAILED, JobRunner
from timeslides.pipeline import RunSpec


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "runs"


def _runner(runs_dir, render, **kw):
    return JobRunner(render, runs_dir, **kw)


def _spec(**over):
    base = dict(group_ids=("a",), modes=("REAL",))
    base.update(over)
    return RunSpec(**base)


def _wait(runner, job, timeout=10.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if runner.get(job.id).status in (DONE, FAILED):
            return runner.get(job.id)
        deadline.wait(0.02)
    raise AssertionError(f"job {job.id} did not finish; status "
                         f"{runner.get(job.id).status}")


def test_a_submitted_job_runs_and_writes_its_report(runs_dir):
    runner = _runner(runs_dir, lambda spec, progress: "<html>ok</html>")
    try:
        job, joined = runner.submit(_spec(), "Group A")
        assert joined is False
        done = _wait(runner, job)
        assert done.status == DONE
        assert done.path.read_text(encoding="utf-8") == "<html>ok</html>"
        assert done.as_dict()["reportUrl"] == f"/api/runs/{job.id}/report"
    finally:
        runner.shutdown()


def test_an_identical_spec_joins_the_running_job_instead_of_rendering_twice(runs_dir):
    """Three people opening the same group at once must not cost three renders
    worth of UDL calls for one result."""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def render(spec, progress):
        calls.append(1)
        started.set()
        release.wait(5)
        return "<html>x</html>"

    runner = _runner(runs_dir, render)
    try:
        first, _ = runner.submit(_spec(), "A")
        assert started.wait(5)
        second, joined = runner.submit(_spec(), "A")
        assert joined is True
        assert second.id == first.id
        release.set()
        _wait(runner, first)
        assert len(calls) == 1
    finally:
        release.set()
        runner.shutdown()


def test_a_different_spec_is_a_different_job(runs_dir):
    runner = _runner(runs_dir, lambda spec, progress: "<html>x</html>")
    try:
        a, _ = runner.submit(_spec(), "A")
        b, joined = runner.submit(_spec(modes=("REAL", "SIM")), "A")
        assert joined is False
        assert b.id != a.id
    finally:
        runner.shutdown()


def test_a_completed_job_is_reused_rather_than_re_rendered(runs_dir):
    calls = []
    runner = _runner(runs_dir, lambda s, p: (calls.append(1), "<html>x</html>")[1])
    try:
        first, _ = runner.submit(_spec(), "A")
        _wait(runner, first)
        again, joined = runner.submit(_spec(), "A")
        assert joined is True and again.id == first.id
        assert len(calls) == 1
    finally:
        runner.shutdown()


def test_a_failing_render_marks_the_job_not_the_process(runs_dir):
    def render(spec, progress):
        raise ComputeError("no usable data")

    runner = _runner(runs_dir, render)
    try:
        job, _ = runner.submit(_spec(), "A")
        done = _wait(runner, job)
        assert done.status == FAILED
        assert "ComputeError" in done.error
        assert "no usable data" in done.error
        assert done.as_dict()["reportUrl"] is None
    finally:
        runner.shutdown()


def test_an_unexpected_exception_still_lands_on_the_job(runs_dir):
    """If it vanished into the worker thread the page would poll forever."""
    def render(spec, progress):
        raise ZeroDivisionError("oops")

    runner = _runner(runs_dir, render)
    try:
        job, _ = runner.submit(_spec(), "A")
        done = _wait(runner, job)
        assert done.status == FAILED
        assert "ZeroDivisionError" in done.error
    finally:
        runner.shutdown()


def test_a_failed_spec_is_retried_rather_than_returning_the_stale_failure(runs_dir):
    """Single-flight must not cache failures, or a transient UDL outage would
    poison that spec until the pod restarts."""
    attempts = []

    def render(spec, progress):
        attempts.append(1)
        if len(attempts) == 1:
            raise ComputeError("transient")
        return "<html>ok</html>"

    runner = _runner(runs_dir, render)
    try:
        first, _ = runner.submit(_spec(), "A")
        _wait(runner, first)
        second, joined = runner.submit(_spec(), "A")
        assert joined is False
        assert second.id != first.id
        assert _wait(runner, second).status == DONE
        assert len(attempts) == 2
    finally:
        runner.shutdown()


def test_progress_updates_are_visible_while_the_job_runs(runs_dir):
    seen = threading.Event()

    def render(spec, progress):
        progress("PRC SpacePlane 4", 1, 3)
        seen.set()
        return "<html>x</html>"

    runner = _runner(runs_dir, render)
    try:
        job, _ = runner.submit(_spec(), "A")
        assert seen.wait(5)
        _wait(runner, job)
        assert job.progress["total"] == 3
        assert job.progress["current"] == "PRC SpacePlane 4"
    finally:
        runner.shutdown()


def test_progress_text_is_sanitised(runs_dir):
    """The label is a group name, which is user-supplied."""
    def render(spec, progress):
        progress("a\nFAKE", 1, 1)
        return "<html>x</html>"

    runner = _runner(runs_dir, render)
    try:
        job, _ = runner.submit(_spec(), "A")
        _wait(runner, job)
        assert "\n" not in job.progress["current"]
    finally:
        runner.shutdown()


def test_an_unknown_run_id_is_a_not_found(runs_dir):
    runner = _runner(runs_dir, lambda s, p: "x")
    try:
        with pytest.raises(NotFoundError, match="no run with id"):
            runner.get("nope")
    finally:
        runner.shutdown()


def test_the_registry_is_bounded_and_deletes_the_evicted_reports(runs_dir):
    """Neither the job table nor the volume may grow forever."""
    runner = _runner(runs_dir, lambda s, p: "<html>x</html>", max_remembered=3)
    try:
        jobs = []
        for i in range(6):
            job, _ = runner.submit(_spec(group_ids=(f"g{i}",)), f"G{i}")
            jobs.append(_wait(runner, job))
        assert len(runner.list()) == 3
        remaining = list(runs_dir.glob("*.html"))
        assert len(remaining) == 3
        for old in jobs[:3]:
            assert not old.path.exists()
    finally:
        runner.shutdown()


def test_eviction_never_discards_a_job_that_is_still_running(runs_dir):
    release = threading.Event()

    def render(spec, progress):
        release.wait(5)
        return "<html>x</html>"

    runner = _runner(runs_dir, render, workers=1, max_remembered=1)
    try:
        first, _ = runner.submit(_spec(group_ids=("a",)), "A")
        for i in range(3):
            runner.submit(_spec(group_ids=(f"b{i}",)), "B")
        assert runner.get(first.id) is first          # still tracked
        release.set()
        assert _wait(runner, first).status == DONE
    finally:
        release.set()
        runner.shutdown()


def test_listing_is_newest_first(runs_dir):
    runner = _runner(runs_dir, lambda s, p: "<html>x</html>")
    try:
        a, _ = runner.submit(_spec(group_ids=("a",)), "A")
        _wait(runner, a)
        b, _ = runner.submit(_spec(group_ids=("b",)), "B")
        _wait(runner, b)
        assert [r["id"] for r in runner.list()] == [b.id, a.id]
    finally:
        runner.shutdown()


def test_the_report_write_is_atomic(runs_dir):
    runner = _runner(runs_dir, lambda s, p: "<html>x</html>")
    try:
        job, _ = runner.submit(_spec(), "A")
        _wait(runner, job)
        assert list(runs_dir.glob("*.tmp")) == []
    finally:
        runner.shutdown()
