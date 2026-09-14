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

These tests cover what can be checked without a container runtime. They are
not a substitute for the build, and they were not enough: the image has since
been built and run here, and doing so found two defects no reading of the
script would have caught. The virtualenv's bin/python points at
/usr/local/bin/python, a symlink the script did not copy, so the chroot
verification died with exit 127 and the image's CMD would have done the same
on every pod start. And a `|| true` on the end of the build stage's install
chain swallowed a failed dependency install outright, leaving an empty
virtualenv and a green build.
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
    "usr/bin/perl", "usr/bin/apt", "usr/bin/dpkg", "var/lib/dpkg/status",
    "bin/login", "usr/bin/passwd", "bin/sh",
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


# --------------------------------------------------------------------------- #
#  What the trial build found
#
#  Everything below is a regression test for a defect that survived review of
#  the script and only appeared when the image was built for real.
# --------------------------------------------------------------------------- #
def test_the_interpreter_symlinks_are_copied_not_just_the_binary(script):
    """Exit 127, and the reason the first real build failed.

    /opt/venv/bin/python points at /usr/local/bin/python, which in the base
    image is a symlink to python3, which is a symlink to python3.13. Copying
    only python3.13 leaves the venv's entry point dangling: chroot reports "No
    such file or directory", and the image's CMD, which names that same
    absolute path, would fail identically on every start.
    """
    assert ('for exe in "$BASE"/bin/python "$BASE"/bin/python3 '
            '"$BASE/bin/python$VER"; do') in script
    # Copied as links, so -a rather than -aL: three names, one binary.
    assert 'cp -a "$exe" "$ROOT$BASE/bin/"' in script


def test_the_verification_checks_the_entry_point_resolves(script):
    """The check for the above, inside the script, where it fails the build."""
    assert '"/opt/venv/bin/python"' in script
    assert "the CMD entry point does not resolve" in script


def test_the_build_stage_does_not_swallow_a_failed_install(dockerfile):
    """A trial build with no route to the package index produced an empty
    virtualenv and a green build stage, because `|| true` was on the end of the
    whole chain rather than on the uninstall it was meant to tolerate. pip
    uninstall already exits 0 for a package that is not installed, so the
    tolerance was never needed.
    """
    install = [ln for ln in dockerfile.splitlines() if "pip install" in ln
               or "pip uninstall" in ln]
    assert install, "the build stage no longer installs anything"
    for line in install:
        assert "|| true" not in line, line


def test_the_image_declares_its_packages_to_the_scanner(script):
    """Minimal is not the same as invisible.

    Dropping the Debian userland drops /var/lib/dpkg with it, and a scanner
    that cannot find a package database reports no operating-system packages at
    all. Fourteen Debian libraries remain, libc6 and openssl among them. An
    image that passes because the scanner was blinded is worse than one that
    fails honestly, so the packages that really are present are declared in the
    status.d layout that Syft, Grype and Trivy read.
    """
    assert "status.d" in script
    assert "dpkg-query -s" in script
    # And the distribution, or there is nothing to match an advisory against.
    assert "os-release" in script


def test_the_declaration_is_checked_rather_than_assumed(script):
    """An empty status.d would produce exactly the clean scan it is meant to
    prevent, so the script fails the build if the C library every binary in the
    image links against is not declared."""
    assert "status.d/libc6" in script
    assert "not declared to the scanner" in script


def test_the_package_scan_handles_a_diverted_path(script):
    """dpkg -S prints "diversion by libc6 from: ..." ahead of the real
    ownership line for a diverted path. Cutting at the first colon turned that
    into a package named "diversionbylibc6from", and the build failed on it."""
    assert "grep -v '^diversion '" in script


def test_the_verification_confirms_pip_is_not_in_the_image(script):
    """The scan raises an advisory per pip version it finds, and an image that
    installs nothing has no use for it. Removed in two places, so the check is
    what says one of them worked."""
    assert "pip is importable in the image" in script


# --------------------------------------------------------------------------- #
#  The App Store simulation
#
#  Three pipeline failures in a row were environment differences rather than
#  code defects, and each was cheap to reproduce once named. docker/appstore-sim.sh
#  reproduces them instead of remembering them.
# --------------------------------------------------------------------------- #
SIM = ROOT / "docker" / "appstore-sim.sh"


def test_the_app_store_simulation_exists_and_is_valid_shell():
    import subprocess

    assert SIM.exists(), "the pre-upload simulation is missing"
    done = subprocess.run(["sh", "-n", str(SIM)], capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_simulation_reproduces_every_difference_that_has_bitten():
    """Each line here is a pipeline failure that passed locally first."""
    text = SIM.read_text(encoding="utf-8")
    # The tree is unpacked, not cloned.
    assert 'rm -rf "$WORK/.git"' in text
    # The ingest does not carry the scanner's configuration.
    assert 'rm -f "$WORK/sonar-project.properties"' in text
    # The job container has no git binary, which is not the same thing as
    # having no .git, and is what aborted collection for the whole suite.
    assert "case \"$name\" in git|git-*) continue ;; esac" in text
    # The upload is what git tracks, so an untracked file shows up as missing.
    assert "git ls-files" in text


def test_the_simulation_keeps_the_rest_of_the_userland():
    """Its own first version used an empty PATH and failed twelve tests that
    need find, chmod and rm. That is the simulation breaking the suite, not the
    platform. The job container is a normal Debian userland without git."""
    text = SIM.read_text(encoding="utf-8")
    assert "so this proves nothing" in text, (
        "the simulation must verify that git really is unreachable")
    assert "ln -s" in text, "it should link the rest of the userland, not drop it"
