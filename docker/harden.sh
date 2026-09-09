#!/bin/sh
# Remove every setuid and setgid bit from the image, then prove it.
#
# Run as the last step of the prep stage, immediately before the flatten. The
# order matters: the container image policy reads layer blobs and layer
# history, so a bit set in an earlier layer is still in that layer's blob after
# a later chmod. Stripping here and then collapsing the result into a single
# layer with `COPY --from=prep / /` onto scratch is what actually clears it.
#
# Kept as a file rather than inline in the Dockerfile so it can be tested. See
# tests/test_harden.py, which builds a synthetic rootfs with setuid files and
# checks both that they are cleared and that the assertion fires when they are
# not.
set -eu

ROOT="${HARDEN_ROOT:-/}"

# 1. Clear the bits wherever they appear on this filesystem.
find "$ROOT" -xdev -perm /6000 -type f -exec chmod -s {} + 2>/dev/null || true

# 2. Remove the setuid utilities outright. Nothing here is reachable from an
#    HTTP server that talks to one API, and a deleted binary cannot regress.
for victim in usr/bin/passwd usr/bin/chsh usr/bin/chfn usr/bin/newgrp \
              usr/bin/gpasswd usr/bin/su bin/su usr/bin/mount usr/bin/umount \
              sbin/unix_chkpwd usr/sbin/unix_chkpwd; do
    rm -f "$ROOT$victim" 2>/dev/null || true
done

# 3. Drop what the runtime never reads, to keep the single layer small and the
#    scan's surface to what actually runs.
rm -rf "$ROOT"var/lib/apt/lists/* "$ROOT"var/cache/apt "$ROOT"var/cache/debconf \
       "$ROOT"usr/share/doc "$ROOT"usr/share/man "$ROOT"usr/share/info \
       "$ROOT"root/.cache 2>/dev/null || true
find "$ROOT" -xdev -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT" -xdev -name '*.pyc' -delete 2>/dev/null || true

# 4. The application tree belongs to the runtime user.
[ -d "$ROOT"app ] && chown -R 1000:1000 "$ROOT"app

# 5. Fail the build rather than shipping a policy violation.
remaining="$(find "$ROOT" -xdev -perm /6000 -type f 2>/dev/null || true)"
if [ -n "$remaining" ]; then
    echo "harden.sh: setuid/setgid files remain after strip:" >&2
    echo "$remaining" >&2
    exit 1
fi
echo "harden.sh: no setuid or setgid files remain"
