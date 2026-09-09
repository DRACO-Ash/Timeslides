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
FROM python:3.11-slim-bookworm AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /w
COPY requirements.txt .
# Built into a virtualenv so the runtime rootfs carries the interpreter and the
# dependencies and nothing else: no pip, no setuptools, no build toolchain.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip==25.2 \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel || true

# --------------------------------------------------------------------------- #
#  Stage 2: the rootfs, cleaned
# --------------------------------------------------------------------------- #
FROM python:3.11-slim-bookworm AS prep

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app.py ./
COPY timeslides ./timeslides

# The mount point exists in the image with the right ownership so a volume
# attached over it is writable by uid 1000 even before fsGroup applies.
RUN install -d -o 1000 -g 1000 -m 0770 /data /app/runs

# Strip every setuid and setgid bit and every capability-bearing binary the
# base image ships. The policy rejects an image carrying any of them, and none
# of them are reachable from a process that serves HTTP and calls one API.
RUN set -eux; \
    find / -xdev -perm /6000 -type f -exec chmod -s {} + 2>/dev/null || true; \
    rm -rf /usr/bin/passwd /usr/bin/chsh /usr/bin/chfn /usr/bin/newgrp \
           /usr/bin/gpasswd /usr/bin/su /bin/su /usr/bin/mount /usr/bin/umount \
           /sbin/unix_chkpwd /usr/sbin/unix_chkpwd 2>/dev/null || true; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/cache/debconf \
           /usr/share/doc /usr/share/man /usr/share/info /tmp/* /root/.cache; \
    find / -xdev -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true; \
    find / -xdev -name '*.pyc' -delete 2>/dev/null || true; \
    chown -R 1000:1000 /app; \
    # Prove the strip worked before the flatten, so a policy failure surfaces
    # here with a readable message rather than at the container-scan stage.
    remaining="$(find / -xdev -perm /6000 -type f 2>/dev/null || true)"; \
    if [ -n "$remaining" ]; then \
      echo "setuid/setgid files remain after strip:"; echo "$remaining"; exit 1; \
    fi

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
