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
# that host ("no such host", exit 125). An earlier revision of this file made
# exactly that mistake.
#
# Fully qualified rather than the bare `python:3.12-slim`, because a short name
# goes through the builder's unqualified-search-registries list, which is not
# guaranteed to be configured. `docker.io/library/...` matches the mirror
# prefix unambiguously.
#
# The tag is known good: the platform's own runner uses
# docker.io/library/python:3.12-slim through this same mirror.
#
# Two rules from the App Store container image policy shape this file.
#
# 1. Non-root, specified numerically. USER 1000:1000, not a name, because the
#    policy reads the numeric uid and a name it cannot resolve reads as root.
#
# 2. The policy judges layer blobs and layer history, not just the final
#    filesystem. A setuid bit set in an early layer is still in that layer's
#    blob after a later `chmod -s`, so stripping in place does not clear the
#    finding. The image is therefore flattened: a prep stage builds the whole
#    rootfs and strips it, then a single COPY onto scratch collapses it into
#    one clean layer with no history to scan.
#
# Note what is deliberately absent: there is no `ENV PORT=`. The platform sets
# containerPort 8080 and probes it, and the application reads PORT with 8080 as
# its default. Setting it here is how you end up serving on a port nothing
# probes.

# --------------------------------------------------------------------------- #
#  Stage 1: wheels
# --------------------------------------------------------------------------- #
FROM docker.io/library/python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /w
# requirements-runtime.txt, not requirements.txt. The latter also carries the
# test tooling, because the platform's generated test stage installs only
# requirements.txt and runs pytest from it. None of that belongs in the image:
# it would enlarge the layer and widen what the container scan judges for code
# that never runs in production.
COPY requirements-runtime.txt .
# Built into a virtualenv so the runtime rootfs carries the interpreter and the
# dependencies and nothing else: no pip, no setuptools, no build toolchain.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip==25.2 \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements-runtime.txt \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel || true

# --------------------------------------------------------------------------- #
#  Stage 2: the rootfs, cleaned
# --------------------------------------------------------------------------- #
FROM docker.io/library/python:3.12-slim AS prep

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app.py ./
COPY timeslides ./timeslides

# The mount point exists in the image with the right ownership so a volume
# attached over it is writable by uid 1000 even before fsGroup applies.
RUN install -d -o 1000 -g 1000 -m 0770 /data

# Strip every setuid and setgid bit the base image ships. The policy rejects an
# image carrying any of them, and none are reachable from a process that serves
# HTTP and calls one API.
#
# The last three lines re-scan and fail the build if anything survived, so a
# policy problem surfaces here with a readable message instead of at the
# container-scan stage. The script is carried in a file rather than inline
# because a `#` comment inside a RUN continuation is parser-dependent, and this
# builder is buildah rather than BuildKit.
COPY docker/harden.sh /tmp/harden.sh
RUN sh /tmp/harden.sh && rm -f /tmp/harden.sh

# --------------------------------------------------------------------------- #
#  Stage 3: one flat layer
# --------------------------------------------------------------------------- #
FROM scratch

COPY --from=prep / /

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORAGE_MOUNT_PATH=/data

WORKDIR /app
USER 1000:1000
EXPOSE 8080

# Bound to 0.0.0.0: a container listening on localhost is a degraded pod that
# passes its build and fails every probe. One worker, because render jobs are
# held in process and the group store is one file on one volume; concurrency
# comes from the render thread pool.
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1", \
     "--no-access-log"]
