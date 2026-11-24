"""docs/api.html is a hand-written copy of the route table, and a copy diverges.

These keep it honest against the application itself rather than against a
list restated here: the routes are read from the app FastAPI builds, and the
document is parsed, not grepped, so prose that mentions a path in passing is
not mistaken for a documented route.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from tests.conftest import repo_file


class _Endpoints(HTMLParser):
    """Collect (method, path) from each endpoint card's header."""

    def __init__(self):
        super().__init__()
        self.found, self._in = set(), None
        self._method = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "span" and "m" in classes:
            self._in = "method"
        elif tag == "span" and "path" in classes:
            self._in = "path"

    def handle_endtag(self, tag):
        if tag == "span":
            self._in = None

    def handle_data(self, data):
        text = data.strip()
        if not text or self._in is None:
            return
        if self._in == "method":
            self._method = text
        elif self._in == "path" and self._method:
            self.found.add((self._method, text))
            self._method = None


def _documented() -> set:
    parser = _Endpoints()
    parser.feed(repo_file("docs/api.html", "the API reference").read_text(encoding="utf-8"))
    return parser.found


def _served(tmp_path) -> set:
    from timeslides.api import create_app
    from timeslides.config import load_settings
    from timeslides.jobs import JobRunner

    settings = load_settings({"TIMESLIDES_DEMO": "1", "STORAGE_MOUNT_PATH": str(tmp_path)})
    runner = JobRunner(lambda s, p: "<html/>", settings.runs_path)
    try:
        app = create_app(settings=settings, runner=runner)
        return {(method, route.path) for route in app.routes
                for method in getattr(route, "methods", None) or ()
                if method != "HEAD"}
    finally:
        runner.shutdown()


def test_every_route_the_app_serves_is_documented_and_nothing_else_is(tmp_path):
    served, documented = _served(tmp_path), _documented()
    assert served - documented == set(), "routes missing from docs/api.html"
    assert documented - served == set(), "docs/api.html documents routes that do not exist"


def test_the_reference_says_which_version_it_describes():
    text = repo_file("docs/api.html", "the API reference").read_text(encoding="utf-8")
    assert re.search(r"Version <b>\d+\.\d+\.\d+</b>", text)
