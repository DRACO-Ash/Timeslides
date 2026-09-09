"""Background render jobs.

A run for seven objects across three providers over seven days is tens of UDL
calls and order 1e5 SGP4 propagations and frame conversions. Measured on this
toolchain that is roughly twenty seconds of work, and a slow tenant or a wide
window makes it minutes. That cannot be the body of an HTTP request: the client
times out, a proxy gives up, the user presses refresh and starts a second one.

So a render is a job. POST returns an id straight away, the page polls, and the
finished HTML is written to the persistent volume and served from there.

Two protections the original script did not need:

  * Single-flight. Identical specs share one render. Without it, three people
    opening the same group at once triples the UDL spend for one result.
  * A bounded worker pool and a bounded registry, so a burst of requests cannot
    fan out into unbounded threads or grow the job table forever.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .audit import event, safe
from .errors import NotFoundError, TimeslidesError

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
MAX_REMEMBERED = 50


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    """One render. Mutated only under the registry lock."""

    def __init__(self, job_id: str, spec, label: str):
        self.id = job_id
        self.spec = spec
        self.label = label
        self.status = QUEUED
        self.created = _now()
        self.started = None
        self.finished = None
        self.error = None
        self.path = None
        self.progress = dict(done=0, total=0, current="")

    def as_dict(self) -> dict:
        return dict(id=self.id, status=self.status, label=self.label,
                    created=self.created, started=self.started,
                    finished=self.finished, error=self.error,
                    progress=dict(self.progress),
                    reportUrl=f"/api/runs/{self.id}/report" if self.status == DONE else None)


class JobRunner:
    """Registry plus a small thread pool.

    `render` is injected rather than imported so the HTTP layer, the tests and
    demo mode can all supply their own without this module knowing about the
    UDL.
    """

    def __init__(self, render, runs_path, workers: int = 2,
                 max_remembered: int = MAX_REMEMBERED):
        self.render = render
        self.runs_path = runs_path
        self.max_remembered = max_remembered
        self._jobs: OrderedDict = OrderedDict()
        self._by_key: dict = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="timeslides-render")

    # --- submission -------------------------------------------------------- #
    def submit(self, spec, label: str) -> tuple:
        """Queue a render, or join an identical one already in flight.

        Returns (job, joined). `joined` is True when this call attached to an
        existing run rather than starting a new one, so the caller can say so.
        """
        key = spec.key()
        with self._lock:
            existing_id = self._by_key.get(key)
            existing = self._jobs.get(existing_id) if existing_id else None
            if existing is not None and existing.status in (QUEUED, RUNNING, DONE):
                return existing, True
            job = Job(str(uuid.uuid4()), spec, label)
            self._jobs[job.id] = job
            self._by_key[key] = job.id
            self._evict_locked()
        event("run.queued", run_id=job.id, groups=label)
        self._pool.submit(self._execute, job)
        return job, False

    # --- execution --------------------------------------------------------- #
    def _progress(self, job):
        def report(current, done, total):
            with self._lock:
                job.progress = dict(done=done, total=total, current=safe(current, 80))
        return report

    def _execute(self, job) -> None:
        with self._lock:
            job.status = RUNNING
            job.started = _now()
        try:
            html = self.render(job.spec, self._progress(job))
            path = self._write(job.id, html)
        except TimeslidesError as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}")
        except Exception as exc:                        # noqa: BLE001
            # An unexpected error must still land on the job rather than
            # vanishing into a worker thread, or the page polls a run that
            # never resolves.
            self._fail(job, f"unexpected {type(exc).__name__}: {exc}")
        else:
            with self._lock:
                job.status = DONE
                job.finished = _now()
                job.path = path
                job.progress = dict(job.progress, done=job.progress.get("total", 0))
            event("run.completed", run_id=job.id, groups=job.label,
                  bytes=len(html))

    def _fail(self, job, message: str) -> None:
        with self._lock:
            job.status = FAILED
            job.finished = _now()
            job.error = safe(message, 400)
            # A failed spec must not be joinable, or every later request for it
            # returns the same stale failure instead of retrying.
            if self._by_key.get(job.spec.key()) == job.id:
                self._by_key.pop(job.spec.key(), None)
        event("run.failed", run_id=job.id, groups=job.label, error=job.error)

    def _write(self, job_id: str, html: str):
        self.runs_path.mkdir(parents=True, exist_ok=True)
        path = self.runs_path / f"{job_id}.html"
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(path)
        return path

    # --- reads ------------------------------------------------------------- #
    def get(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"no run with id {safe(job_id)}")
        return job

    def list(self) -> list:
        with self._lock:
            return [j.as_dict() for j in reversed(self._jobs.values())]

    # --- housekeeping ------------------------------------------------------ #
    def _evict_locked(self) -> None:
        """Drop the oldest finished jobs, and their report files with them, so
        neither the registry nor the volume grows without bound."""
        while len(self._jobs) > self.max_remembered:
            _, oldest = self._jobs.popitem(last=False)
            if oldest.status in (QUEUED, RUNNING):
                # Never evict live work; put it back and stop trimming.
                self._jobs[oldest.id] = oldest
                self._jobs.move_to_end(oldest.id, last=False)
                return
            if self._by_key.get(oldest.spec.key()) == oldest.id:
                self._by_key.pop(oldest.spec.key(), None)
            if oldest.path is not None:
                try:
                    oldest.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
