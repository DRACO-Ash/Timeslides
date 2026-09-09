"""docker/harden.sh, tested against a synthetic rootfs.

The image cannot be built in every environment, and the hardening step is the
one piece of custom logic in the Dockerfile: it clears the setuid and setgid
bits the container image policy rejects, and it fails the build if any survive.
Both halves matter. A strip that silently misses something ships a policy
violation; an assertion that never fires is the fail-open defect, mapping
"could not verify" to "passed".

So the script takes HARDEN_ROOT and is exercised here against a directory tree
built to look like the relevant parts of a Debian slim rootfs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "harden.sh"


def _run(root: Path):
    # check=False on purpose: the return code is what these tests inspect.
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                          env={**os.environ, "HARDEN_ROOT": f"{root}/"},
                          check=False)


@pytest.fixture
def rootfs(tmp_path):
    """A miniature rootfs with the things the script is meant to act on."""
    root = tmp_path / "rootfs"
    for d in ("usr/bin", "bin", "sbin", "usr/sbin", "usr/share/doc",
              "usr/share/man", "var/lib/apt/lists", "var/cache/apt",
              "app/timeslides", "opt/venv/lib", "root/.cache", "usr/lib"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # Setuid and setgid binaries, as a Debian base image ships them.
    for name, bit in (("usr/bin/passwd", stat.S_ISUID),
                      ("usr/bin/su", stat.S_ISUID),
                      ("usr/bin/chsh", stat.S_ISUID),
                      ("usr/bin/wall", stat.S_ISGID),
                      ("usr/lib/unrelated-setuid", stat.S_ISUID)):
        p = root / name
        p.write_text("#!/bin/true\n")
        p.chmod(0o755 | bit)

    # Ordinary files that must survive untouched.
    (root / "usr/bin/python3").write_text("interpreter")
    (root / "usr/bin/python3").chmod(0o755)
    (root / "app/app.py").write_text("app = 1")
    (root / "opt/venv/lib/mod.py").write_text("x = 1")

    # Cruft the script should remove.
    (root / "usr/share/doc/readme").write_text("docs")
    (root / "var/lib/apt/lists/list").write_text("lists")
    (root / "root/.cache/thing").write_text("cache")
    pyc = root / "app/timeslides/mod.pyc"
    pyc.write_text("bytecode")
    cache = root / "app/timeslides/__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_text("bytecode")
    return root


def _setuid_files(root: Path) -> list:
    out = []
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mode & (stat.S_ISUID | stat.S_ISGID):
            out.append(str(path.relative_to(root)))
    return sorted(out)


def test_the_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_every_setuid_and_setgid_bit_is_cleared(rootfs):
    assert _setuid_files(rootfs), "fixture did not create any setuid files"
    done = _run(rootfs)
    assert done.returncode == 0, done.stderr
    assert _setuid_files(rootfs) == []


def test_the_known_setuid_utilities_are_deleted_outright(rootfs):
    _run(rootfs)
    for gone in ("usr/bin/passwd", "usr/bin/su", "usr/bin/chsh"):
        assert not (rootfs / gone).exists(), gone


def test_a_setuid_file_outside_the_known_list_is_still_stripped(rootfs):
    """The deletion list is a convenience. The find is what makes it safe."""
    _run(rootfs)
    survivor = rootfs / "usr/lib/unrelated-setuid"
    assert survivor.exists(), "it should be stripped, not deleted"
    assert not survivor.stat().st_mode & (stat.S_ISUID | stat.S_ISGID)


def test_a_setgid_bit_is_cleared_not_just_setuid(rootfs):
    _run(rootfs)
    wall = rootfs / "usr/bin/wall"
    if wall.exists():
        assert not wall.stat().st_mode & stat.S_ISGID


def test_the_interpreter_and_the_application_survive(rootfs):
    _run(rootfs)
    assert (rootfs / "usr/bin/python3").read_text() == "interpreter"
    assert (rootfs / "app/app.py").read_text() == "app = 1"
    assert (rootfs / "opt/venv/lib/mod.py").read_text() == "x = 1"


def test_documentation_caches_and_bytecode_are_removed(rootfs):
    _run(rootfs)
    assert not (rootfs / "usr/share/doc/readme").exists()
    assert not (rootfs / "root/.cache/thing").exists()
    assert not (rootfs / "app/timeslides/mod.pyc").exists()
    assert not (rootfs / "app/timeslides/__pycache__").exists()
    assert list((rootfs / "var/lib/apt/lists").glob("*")) == []


def test_the_application_tree_is_owned_by_the_runtime_user(rootfs):
    if os.geteuid() != 0:
        pytest.skip("chown needs root")
    _run(rootfs)
    assert (rootfs / "app").stat().st_uid == 1000
    assert (rootfs / "app/app.py").stat().st_gid == 1000


def test_it_reports_success_when_the_tree_is_clean(rootfs):
    done = _run(rootfs)
    assert "no setuid or setgid files remain" in done.stdout


def test_it_is_idempotent(rootfs):
    assert _run(rootfs).returncode == 0
    second = _run(rootfs)
    assert second.returncode == 0
    assert _setuid_files(rootfs) == []


def test_the_assertion_fires_when_a_setuid_file_survives(rootfs, monkeypatch):
    """The half that stops this failing open.

    A strip that quietly misses something would ship a policy violation and the
    build would go green. Simulated by making the strip a no-op, leaving the
    final scan to catch it.
    """
    stripped = SCRIPT.read_text(encoding="utf-8").replace(
        "find \"$ROOT\" -xdev -perm /6000 -type f -exec chmod -s {} + 2>/dev/null || true",
        ": no-op, simulating a strip that missed")
    broken = rootfs.parent / "harden-broken.sh"
    broken.write_text(stripped, encoding="utf-8")
    done = subprocess.run(["sh", str(broken)], capture_output=True, text=True,
                          env={**os.environ, "HARDEN_ROOT": f"{rootfs}/"},
                          check=False)
    assert done.returncode == 1, "the build should have failed"
    assert "setuid/setgid files remain" in done.stderr
    assert "unrelated-setuid" in done.stderr


def test_it_survives_a_rootfs_missing_the_optional_paths(tmp_path):
    """A base image without /app or the doc directories must not fail it."""
    bare = tmp_path / "bare"
    (bare / "usr/bin").mkdir(parents=True)
    (bare / "usr/bin/true").write_text("x")
    done = _run(bare)
    assert done.returncode == 0, done.stderr
