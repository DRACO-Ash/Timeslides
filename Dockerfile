# Base image: reference Docker Hub and let the platform's mirror rewrite it.
#
# The pipeline's containerize stage writes a registry mirror rule before it
# builds anything:
#
#     [[registry]]
#     prefix   = "docker.io"
#     location = "registry.bluestaq.com/<mirror>"
#
# so a docker.io reference is redirected to Harbor and never leaves the estate.
# Naming the internal registry directly does NOT work: that prefix rule only
# matches docker.io, so an explicit registry.bluestaq.com reference bypasses
# the mirror and is resolved directly, and the build container has no DNS for
# that host ("no such host", exit 125). An earlier revision made that mistake.
#
# Fully qualified rather than the bare `python:3.13.15-slim`, because a short
# name goes through the builder's unqualified-search-registries list, which is
# not guaranteed to be configured.
#
# THE TAG IS PINNED, AND WHY IT MOVED FROM 3.12
#
# The container scan reported CVE-2026-4224, CVE-2026-3644 and CVE-2026-7210
# against the interpreter in python:3.12-slim, each fixed only in 3.13.13 or
# later; no patch to a 3.12 image can clear them. 3.13.15 is at or above every
# "fixed in" version the scan named for the 3.13 series.
#
# The whole runtime stack was installed and exercised on 3.13 before this
# change: numpy, astropy, sgp4, fastapi, pydantic, plotly, requests and uvicorn
# all resolve to cp313 or abi3 wheels, and the application served a full render
# of 4,545,975 bytes on it. Pinned to the patch version rather than 3.13, so
# the image that was reasoned about is the image that gets built.
ARG PYTHON_TAG=3.13.15-slim

# --------------------------------------------------------------------------- #
#  Stage 1: wheels
# --------------------------------------------------------------------------- #
FROM docker.io/library/python:${PYTHON_TAG} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /w
# requirements-runtime.txt, not requirements.txt. The latter also carries the
# test tooling, because the platform's generated test stage installs only
# requirements.txt and runs pytest from it. None of that belongs in the image.
COPY requirements-runtime.txt .
# Built into a virtualenv so the runtime carries the interpreter and the
# dependencies and nothing else: no pip, no setuptools, no build toolchain.
#
# No `|| true` on the end of this chain. It was there to tolerate a package
# that is not installed, which `pip uninstall` already tolerates: it warns,
# skips and exits 0. What it actually did was swallow a failed dependency
# install. A trial build with no route to the index produced an empty
# virtualenv, a green build stage, and an image that could not import numpy.
# The only thing that noticed was the chroot verification three stages later.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements-runtime.txt \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

# --------------------------------------------------------------------------- #
#  Stage 2: security updates, then a rootfs of only what is needed
# --------------------------------------------------------------------------- #
FROM docker.io/library/python:${PYTHON_TAG} AS prep

# Apply the distribution's security updates before anything is copied out.
#
# This is what clears the seven critical findings that blocked the image:
# CVE-2026-5450 in libc6 and libc-bin, and five in perl-base. Every one of them
# had a fix published in the Debian security repository and no fix in the base
# image, which is the normal state of a base image a few weeks old.
#
# It does not clear the findings with no upstream fix. Those are resolved by
# the package not being in the final image at all; see build-rootfs.sh.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app.py ./
COPY timeslides ./timeslides

# Assemble the runtime rootfs and prove it runs.
#
# The final image used to be `COPY --from=prep / /`, the whole Debian userland.
# A Python web service calls none of perl, util-linux, login, passwd,
# coreutils, tar, gzip, diffutils or apt, and the scan counted every CVE in all
# of them against this application: 7 critical and 62 high, most in packages
# nothing here ever executes. Several of the high findings had no upstream fix,
# so patching could not have resolved them at any version.
#
# The script computes what to keep with ldd rather than guessing, and then
# chroots into the result and imports the whole application. A minimal rootfs
# missing one library builds cleanly and dies on its first request, which is
# worse than a failing scan, so the build is made to prove itself.
COPY docker/build-rootfs.sh /tmp/build-rootfs.sh
RUN sh /tmp/build-rootfs.sh

# Strip every setuid and setgid bit, on files and on directories.
#
# The policy rejects an image carrying any of them, and none are reachable from
# a process that serves HTTP and calls one API. The scan also reported SGID on
# the directory /var/mail (mode 0o42775), which an earlier version of this
# script did not look for because it only examined files.
#
# The last lines re-scan and fail the build if anything survived, so a policy
# problem surfaces here with a readable message instead of at the scan stage.
# Carried in a file rather than inline because a `#` comment inside a RUN
# continuation is parser-dependent, and this builder is buildah, not BuildKit.
COPY docker/harden.sh /tmp/harden.sh
RUN HARDEN_ROOT=/rootfs sh /tmp/harden.sh && rm -f /tmp/harden.sh /tmp/build-rootfs.sh

# --------------------------------------------------------------------------- #
#  Stage 3: one flat layer
#
#  The policy judges layer blobs and layer history, not just the final
#  filesystem: a setuid bit set in an early layer is still in that layer's blob
#  after a later chmod. A single COPY onto scratch collapses everything into
#  one clean layer with no history to scan.
# --------------------------------------------------------------------------- #
FROM scratch

COPY --from=prep /rootfs /

ENV PATH=/opt/venv/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORAGE_MOUNT_PATH=/data

WORKDIR /app
# Numeric, not a name: the policy reads the numeric uid and a name it cannot
# resolve reads as root.
USER 1000:1000
EXPOSE 8080

# Note what is deliberately absent: there is no `ENV PORT=`. The platform sets
# containerPort 8080 and probes it, and the application reads PORT with 8080 as
# its default. Setting it here is how you end up serving on a port nothing
# probes.
#
# Bound to 0.0.0.0: a container listening on localhost is a degraded pod that
# passes its build and fails every probe. One worker, because render jobs are
# held in process and the group store is one file on one volume; concurrency
# comes from the render thread pool.
#
# Exec form with an absolute path, because there is no shell in this image.
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1", \
     "--no-access-log"]
