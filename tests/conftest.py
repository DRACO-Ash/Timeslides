from __future__ import annotations


import pytest

from timeslides.config import Settings
from timeslides.ratelimit import TokenBucket


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", raise_on_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records requests and replays queued responses. No network, ever."""

    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls = []
        self.headers = {}
        self.auth = None

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(url=url, params=params or {}, timeout=timeout))
        if self.raises:
            raise self.raises
        if not self.responses:
            return FakeResponse(200, [])
        nxt = self.responses.pop(0)
        # Duck-typed rather than isinstance: pytest can load this module twice
        # (once as the conftest plugin, once as tests.conftest), which makes two
        # distinct FakeResponse classes and an isinstance check that silently
        # treats a response object as a JSON payload.
        return nxt if hasattr(nxt, "status_code") else FakeResponse(200, nxt)


@pytest.fixture
def settings(tmp_path):
    return Settings(udl_base="https://udl.test", udl_user="u", udl_pass="pw-should-not-appear",
                    storage_path=tmp_path, max_results=500)


@pytest.fixture
def instant_bucket():
    """A bucket with a fake clock, so rate-limit waits never sleep in tests."""
    now = [0.0]

    def sleep(delay):
        now[0] += delay

    return TokenBucket(600, clock=lambda: now[0], sleep=sleep)


@pytest.fixture
def client(settings, instant_bucket):
    from timeslides.udl import UDLClient

    def build(responses=None, raises=None):
        session = FakeSession(responses, raises)
        c = UDLClient(settings, session=session, bucket=instant_bucket)
        return c, session

    return build


def sv_record(epoch="2026-06-24T00:00:00.000Z", frame="J2000", n=1.0):
    return {"epoch": epoch, "xpos": n, "ypos": 2 * n, "zpos": 3 * n,
            "xvel": 4 * n, "yvel": 5 * n, "zvel": 6 * n,
            **({"referenceFrame": frame} if frame else {})}
