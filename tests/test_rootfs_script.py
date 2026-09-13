"""The rootfs assembly script, checked without needing a container runtime.

The container scan failed the image on 7 critical and 62 high vulnerabilities,
most of them in packages a Python web service never calls: perl, util-linux,
login, coreutils, tar, gzip, apt. They were in the image because the final
layer was `COPY --from=prep / /`, the whole Debian userland. Several of the
high findings had no upstream fix at all, so patching could never have cleared
them; the package had to not be there.

docker/build-rootfs.sh assembles only what the application needs. It verifies
itself at build time by chrooting into the result and importing the whole
application, which is the real guarantee: a rootfs missing one library builds
cleanly and dies on its first request, and that is worse than a failing scan.

These tests cover what can be checked without a container runtime, which is
this environment's limit. They are not a substitute for the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docker" / "build-rootfs.sh"
DOCKERFILE = ROOT / "Dockerfile"


@pytest.fixture(scope="module")
def script():
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerfile():
    return DOCKERFILE.read_text(encoding="utf-8")


def test_the_script_is_valid_shell():
    """Caught here rather than at build time, where the feedback is a pipeline
    run away."""
    import subprocess

    done = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_final_image_is_built_from_the_rootfs_not_the_whole_userland(dockerfile):
    """The defect itself. `COPY --from=prep / /` is what put perl, util-linux,
    login, coreutils, tar, gzip and apt into a Python web service."""
    # Instructions only: the comment above that line explains the old form and
    # would match a naive search of the whole file, which is the same trap the
    # standards compendium fell into twice.
    instructions = [line.strip() for line in dockerfile.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    copies = [line for line in instructions if line.startswith("COPY --from=prep")]
    assert copies == ["COPY --from=prep /rootfs /"], copies


def test_the_build_applies_the_distribution_security_updates(dockerfile):
    """What clears the seven criticals that blocked the image: CVE-2026-5450 in
    libc6 and libc-bin, and five in perl-base, all fixed in the Debian security
    repository and none fixed in a base image a few weeks old."""
    instructions = "\n".join(line for line in dockerfile.splitlines()
                              if not line.lstrip().startswith("#"))
    assert "apt-get update" in instructions
    assert "apt-get upgrade" in instructions


def test_the_base_image_is_pinned_to_a_patch_version(dockerfile):
    """3.12 could not clear CVE-2026-4224, CVE-2026-3644 or CVE-2026-7210,
    each fixed only in 3.13.13 or later. Pinned to the patch rather than the
    minor so the image built is the one that was reasoned about."""
    tag = re.search(r"ARG PYTHON_TAG=(\S+)", dockerfile)
    assert tag, "the base tag should be a build argument"
    version = tag.group(1)
    assert re.match(r"^3\.\d+\.\d+-slim$", version), version
    major, minor, patch = (int(p) for p in version.split("-")[0].split("."))
    assert (major, minor) >= (3, 13), f"{version} cannot clear the Python CVEs"
    if (major, minor) == (3, 13):
        assert patch >= 15, f"{version} is below the fixed-in versions the scan named"


def test_the_base_image_still_goes_through_the_registry_mirror(dockerfile):
    """Naming the internal registry directly bypasses the mirror rule, which
    only matches docker.io, and the build container has no DNS for that host.
    An earlier revision made exactly that mistake and failed with exit 125."""
    instructions = "\n".join(line for line in dockerfile.splitlines()
                              if not line.lstrip().startswith("#"))
    assert "docker.io/library/python:" in instructions
    assert "registry.bluestaq.com" not in instructions


@pytest.mark.parametrize("unwanted", [
    "usr/bin/perl", "usr/bin/apt", "usr/bin/dpkg", "var/lib/dpkg",
    "bin/login", "usr/bin/passwd",
])
def test_the_script_refuses_to_ship_the_packages_that_caused_the_findings(
        script, unwanted):
    """Asserted in the script itself, so a change that reintroduces one fails
    the build rather than the scan."""
    assert unwanted in script


def test_the_script_refuses_a_rootfs_nested_inside_itself(script):
    """A real defect, found by running the script rather than reading it.

    ldd reports a library's path relative to the copy it is inspecting, so the
    closure copied numpy's bundled libraries to $ROOT$ROOT and produced a 31 MB
    /rootfs/rootfs. The image still built and still ran, so nothing else would
    have noticed.
    """
    assert 'case "$lib" in "$ROOT"/*) continue ;; esac' in script
    assert '"$ROOT$ROOT"' in script


def test_the_script_removes_the_extensions_whose_libraries_have_no_fix(script):
    """ncurses and libuuid carry high findings with no upstream fix, so the
    only resolution is for the library not to be in the image. They are there
    because of stdlib extension modules this application never imports."""
    for ext in ("_uuid", "_sqlite3", "_curses", "readline", "_dbm"):
        assert ext in script, ext


def test_the_script_removes_pip_from_both_site_packages_trees(script):
    """The scan raised five advisories against the pip in the base
    interpreter's site-packages, which the virtualenv's own removal never
    touched. An image that installs nothing needs neither copy."""
    assert 'rm -rf "$STDLIB/site-packages"' in script
    assert "site-packages/pip" in script


def test_the_script_verifies_itself_by_running_the_application(script):
    """The reason this change is safe to make without a container runtime to
    test it in. A minimal rootfs missing one library builds cleanly and dies on
    its first request; the build is made to prove otherwise."""
    assert "chroot" in script
    assert "import numpy, astropy, sgp4, fastapi" in script
    assert "compute_series" in script, "the physics has to be exercised, not just imported"
    assert "import app" in script


def test_the_verification_checks_the_application_still_fails_closed(script):
    """Without UDL credentials the application refuses to start, by design.
    That is the one behaviour a misconfigured deployment depends on, and the
    build is the right place to confirm it survived into the image."""
    assert "the image would start without UDL credentials" in script


def test_the_verification_confirms_the_removed_extensions_are_actually_gone(script):
    """Otherwise the check passes just as happily on an image that still
    carries them, and the finding comes back at the next scan."""
    assert "should have been removed from the image" in script


def test_the_image_has_no_shell_so_the_command_is_exec_form(dockerfile):
    """Shell form would need /bin/sh, which this rootfs deliberately lacks."""
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD")]
    assert cmd and cmd[0].startswith('CMD ["'), cmd
    assert "/opt/venv/bin/python" in dockerfile


def test_the_runtime_user_is_numeric(dockerfile):
    """The policy reads the numeric uid, and a name it cannot resolve reads as
    root. The rootfs carries an /etc/passwd anyway, but the image must not
    depend on it being readable."""
    assert "USER 1000:1000" in dockerfile
