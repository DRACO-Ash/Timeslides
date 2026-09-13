#!/bin/sh
# Assemble the smallest rootfs that can run this application, and prove it runs.
#
# WHY THIS EXISTS
#
# The image used to be built by copying the whole Debian userland into the
# final layer. A Python web service needs none of perl, util-linux, login,
# passwd, coreutils, tar, gzip, diffutils or apt, and the container scan
# counted every CVE in all of them against us: 7 critical and 62 high, of which
# the majority were in packages this application never calls.
#
# Worse, several of the high findings had no upstream fix at all. An
# `apt-get upgrade` clears the seven blocking criticals and cannot touch those,
# so the only way to resolve them is for the package not to be in the image.
#
# So the rootfs is built from what the application actually needs: the
# interpreter, the standard library, the virtualenv, the application, and the
# transitive closure of shared libraries those depend on, computed with ldd
# rather than guessed.
#
# WHY IT VERIFIES ITSELF
#
# A minimal rootfs that is missing one library produces an image that builds
# cleanly and dies on its first request. That is a worse outcome than a failing
# scan. The last step therefore chroots into the assembled rootfs and imports
# every runtime module and the application itself, so a missing library fails
# the build, loudly, in the pipeline, rather than failing a pod in production.
set -eu

ROOT=/rootfs
VENV=/opt/venv
PY="$VENV/bin/python"

BASE=$("$PY" -c 'import sys; print(sys.base_prefix)')
VER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')

echo "assembling a minimal rootfs for python $VER from $BASE"
mkdir -p "$ROOT"

# --------------------------------------------------------------------------- #
#  The application, its virtualenv, the interpreter and the standard library
# --------------------------------------------------------------------------- #
mkdir -p "$ROOT/opt" "$ROOT$BASE/bin" "$ROOT$BASE/lib"
cp -a "$VENV" "$ROOT/opt/venv"
cp -a /app "$ROOT/app"

# pip, setuptools and wheel are removed in the build stage, but an image that
# installs nothing has no use for them either way and the scan raises an
# advisory per pip version. Removed here as well so this script produces the
# same rootfs whatever it is handed.
rm -rf "$ROOT/opt/venv/lib"/python*/site-packages/pip \
       "$ROOT/opt/venv/lib"/python*/site-packages/pip-*.dist-info \
       "$ROOT/opt/venv/lib"/python*/site-packages/setuptools \
       "$ROOT/opt/venv/lib"/python*/site-packages/setuptools-*.dist-info \
       "$ROOT/opt/venv/lib"/python*/site-packages/wheel \
       "$ROOT/opt/venv/lib"/python*/site-packages/wheel-*.dist-info \
       "$ROOT/opt/venv/bin/pip" "$ROOT/opt/venv/bin/pip3"* 2>/dev/null || true
# The interpreter, and the two symlinks in front of it.
#
# The virtualenv's own bin/python points at /usr/local/bin/python, which in the
# base image is a relative symlink to python3, which points at python3.13. A
# first version copied only python3.13, so /opt/venv/bin/python dangled: the
# chroot verification failed with "No such file or directory" and exit 127,
# and the image's CMD would have failed the same way on every pod start. The
# links are copied as links, which is why -a and not -aL.
for exe in "$BASE"/bin/python "$BASE"/bin/python3 "$BASE/bin/python$VER"; do
    [ -e "$exe" ] || [ -L "$exe" ] || continue
    cp -a "$exe" "$ROOT$BASE/bin/"
done
cp -a "$BASE/lib/python$VER" "$ROOT$BASE/lib/"
for lib in "$BASE"/lib/libpython*; do
    [ -e "$lib" ] && cp -a "$lib" "$ROOT$BASE/lib/"
done

# The standard library ships a good deal a web service never reaches.
STDLIB="$ROOT$BASE/lib/python$VER"
rm -rf "$STDLIB/test" "$STDLIB/idlelib" "$STDLIB/tkinter" "$STDLIB/turtledemo" \
       "$STDLIB/lib2to3" "$STDLIB/ensurepip" "$STDLIB/distutils"

# The base interpreter's own site-packages carries pip, setuptools and wheel.
# The virtualenv had them removed already; these are the copies the scan found
# and reported five advisories against, none of which a running service needs
# because nothing in the image installs anything.
rm -rf "$STDLIB/site-packages"
mkdir -p "$STDLIB/site-packages"

# Extension modules with no path into this application, removed so the shared
# libraries behind them need not be shipped either. This is not tidiness: the
# ncurses and libuuid findings have no upstream fix at all, so the only way to
# resolve them is for the library not to be there.
#
#   _uuid          the uuid module falls back to os.urandom; verified
#   _sqlite3       nothing here uses sqlite
#   _dbm, _gdbm    nor dbm
#   _curses, _curses_panel, readline   a service has no terminal
#
# Between them these drop libuuid, libsqlite3, libncursesw, libtinfo,
# libreadline and libdb from the closure. The verification at the end of this
# script imports the whole application without them, so the claim is tested
# rather than asserted.
for ext in _uuid _sqlite3 _dbm _gdbm _curses _curses_panel readline; do
    rm -f "$STDLIB/lib-dynload/$ext".cpython-*.so
done

find "$ROOT" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --------------------------------------------------------------------------- #
#  The shared libraries those need, to a fixed point
#
#  Computed rather than listed. A list goes stale the moment a dependency adds
#  an extension module, and the failure mode of a stale list is an image that
#  starts and then cannot import numpy.
# --------------------------------------------------------------------------- #
resolve_libs() {
    find "$ROOT" -type f \( -name "*.so" -o -name "*.so.*" -o -perm -u+x \) \
        -print 2>/dev/null | while read -r f; do
        case "$(head -c 4 "$f" 2>/dev/null | od -An -c | tr -d ' ')" in
            *177ELF*) ldd "$f" 2>/dev/null ;;
        esac
    done | awk '/=>/ && $3 ~ /^\// { print $3 } /^\s*\/lib/ { print $1 }' | sort -u
}

round=0
while [ "$round" -lt 8 ]; do
    round=$((round + 1))
    added=0
    for lib in $(resolve_libs); do
        # A library already inside the rootfs is one we have placed there, and
        # ldd reports it by its in-rootfs path because $ORIGIN resolves
        # relative to the copy it is inspecting. Without this, numpy's bundled
        # libraries were copied to $ROOT$ROOT/..., producing a 31 MB
        # /rootfs/rootfs that the trial run made visible.
        case "$lib" in "$ROOT"/*) continue ;; esac
        [ -e "$ROOT$lib" ] && continue
        [ -e "$lib" ] || continue
        mkdir -p "$ROOT$(dirname "$lib")"
        cp -aL "$lib" "$ROOT$lib"
        added=$((added + 1))
    done
    echo "  round $round: copied $added shared libraries"
    [ "$added" -eq 0 ] && break
done

# The dynamic loader itself is not reported by ldd as a dependency of anything.
for loader in /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-aarch64.so.1; do
    if [ -e "$loader" ]; then
        mkdir -p "$ROOT$(dirname "$loader")"
        cp -aL "$loader" "$ROOT$loader"
    fi
done
[ -e /etc/ld.so.cache ] && { mkdir -p "$ROOT/etc"; cp -a /etc/ld.so.cache "$ROOT/etc/"; }

# --------------------------------------------------------------------------- #
#  Tell the scanner what is still in here
#
#  Dropping the whole Debian userland removes the package database with it, and
#  a scanner that cannot find /var/lib/dpkg reports no operating-system
#  packages at all. That is not the same thing as having none: fourteen Debian
#  shared libraries remain, libc6 and openssl among them, and they carry
#  whatever they carry. An image that is clean because the scanner has been
#  blinded is worse than one that fails honestly, and it is not something to
#  hand a security manager.
#
#  So the packages whose files actually survive are declared, in the
#  /var/lib/dpkg/status.d layout that distroless images use and that Syft,
#  Grype and Trivy all read. The result is an image that is both minimal and
#  assessable: the scan sees exactly the nine packages that are present, and
#  the apt-get upgrade in the stage above is what keeps them patched.
#
#  This ships the metadata only. There is no dpkg or apt binary in the image
#  and nothing can install anything.
# --------------------------------------------------------------------------- #
STATUSD="$ROOT/var/lib/dpkg/status.d"
mkdir -p "$STATUSD"

# Every file that came from the distribution rather than from the interpreter,
# the virtualenv or the application: those are the ones dpkg knows about.
owned_packages() {
    find "$ROOT" -type f -print 2>/dev/null | sed "s|^$ROOT||" | while read -r path; do
        case "$path" in
            "$BASE"/*|/opt/venv/*|/app/*|/etc/*|/tmp/*|/data/*|/var/*) continue ;;
        esac
        # The copy sits at the path ldd reported, which may be the symlink
        # dpkg records or the real file behind it. Ask about both.
        # A diverted path makes dpkg -S print "diversion by libc6 from: ..."
        # ahead of the real ownership line. Cutting at the first colon turned
        # that into a package named "diversionbylibc6from", which dpkg-query
        # then refused to describe. The diversion lines are dropped; the
        # ownership line that follows them is the answer.
        for p in "$path" "$(readlink -f "$path" 2>/dev/null || echo "$path")"; do
            dpkg -S "$p" 2>/dev/null | grep -v '^diversion ' | cut -d: -f1
        done
    done | tr ',' '\n' | tr -d ' ' | grep -v '^$' | sort -u
}

packages=$(owned_packages)
if [ -z "$packages" ]; then
    echo "build-rootfs.sh: no owning packages found for the shipped libraries" >&2
    exit 1
fi
for pkg in $packages; do
    dpkg-query -s "$pkg" > "$STATUSD/$pkg" 2>/dev/null || {
        echo "build-rootfs.sh: dpkg knows $pkg but will not describe it" >&2
        exit 1
    }
done
echo "  declared $(echo "$packages" | wc -w) distribution packages for the scanner"

# The scanner needs the distribution and release to match an advisory to a
# version. Without os-release it has package names and nothing to match against.
for rel in /etc/os-release /usr/lib/os-release; do
    if [ -e "$rel" ]; then
        mkdir -p "$ROOT$(dirname "$rel")"
        cp -aL "$rel" "$ROOT$rel"
    fi
done

# --------------------------------------------------------------------------- #
#  The few files from /etc a running process genuinely reads
# --------------------------------------------------------------------------- #
mkdir -p "$ROOT/etc" "$ROOT/etc/ssl"
# uid 1000 has to resolve to a name, or getpwuid fails and some libraries with
# it. Written rather than copied, so the image carries exactly one account.
cat > "$ROOT/etc/passwd" <<'PASSWD'
root:x:0:0:root:/root:/sbin/nologin
app:x:1000:1000:timeslides:/app:/sbin/nologin
PASSWD
cat > "$ROOT/etc/group" <<'GROUP'
root:x:0:
app:x:1000:
GROUP
printf 'hosts: files dns\n' > "$ROOT/etc/nsswitch.conf"
# TLS to the UDL. requests carries certifi in the virtualenv, but anything
# reaching for the system store must find it rather than fail open.
[ -d /etc/ssl/certs ] && cp -a /etc/ssl/certs "$ROOT/etc/ssl/"
[ -d /usr/share/ca-certificates ] && {
    mkdir -p "$ROOT/usr/share"; cp -a /usr/share/ca-certificates "$ROOT/usr/share/"; }

# --------------------------------------------------------------------------- #
#  Writable places the runtime expects
# --------------------------------------------------------------------------- #
mkdir -p "$ROOT/tmp" && chmod 1777 "$ROOT/tmp"
# The mount point exists with the right ownership so a volume attached over it
# is writable by uid 1000 even before fsGroup applies.
mkdir -p "$ROOT/data" && chown 1000:1000 "$ROOT/data" && chmod 0770 "$ROOT/data"
chown -R 1000:1000 "$ROOT/app"

# --------------------------------------------------------------------------- #
#  Prove it works, here, before it becomes an image
# --------------------------------------------------------------------------- #
echo "verifying the assembled rootfs"
cat > "$ROOT/tmp/verify.py" <<'VERIFY'
import os
import sys
sys.path.insert(0, "/app")
# Every third-party package the service imports at runtime, and the standard
# library modules that are C extensions and so depend on a shared library that
# could have been missed.
# sqlite3, curses and readline are deliberately absent; see the removals above.
import ssl, zlib, bz2, lzma, hashlib, ctypes, socket, uuid, json
# uuid must still work, because the group store names every group with one.
assert uuid.uuid4() is not None
import numpy, astropy, sgp4, fastapi, pydantic, plotly, requests, uvicorn
import timeslides.api, timeslides.physics, timeslides.pipeline, timeslides.storage

# The application fails closed without UDL credentials, by design, and it does
# so at import. Check that first: it is the one behaviour a misconfigured
# deployment depends on, and proving it here costs one import.
from timeslides.config import load_settings
from timeslides.errors import ConfigError
try:
    load_settings({})
except ConfigError:
    pass
else:
    raise SystemExit("the image would start without UDL credentials")

os.environ["TIMESLIDES_DEMO"] = "1"
import app
assert app.app is not None
# And the extensions that were removed really are gone, so this check cannot
# quietly pass on an image that still carries them.
# The CMD names /opt/venv/bin/python by absolute path. That is a symlink into
# the interpreter the rootfs carries, and a dangling one produces a container
# that exits 127 before a single line of this application runs.
assert os.path.exists("/opt/venv/bin/python"), "the CMD entry point does not resolve"
# pip has no business in an image that installs nothing, and the scan raises an
# advisory per version it finds. It is removed in two places; this is the check
# that one of them worked.
for absent in ("/opt/venv/bin/pip", "/usr/local/bin/pip", "/usr/local/bin/pip3"):
    if os.path.lexists(absent):
        raise SystemExit("%s is in the image and should not be" % absent)
try:
    import pip
except ImportError:
    pass
else:
    raise SystemExit("pip is importable in the image")

for gone in ("_sqlite3", "_uuid", "readline", "_curses"):
    try:
        __import__(gone)
    except ImportError:
        pass
    else:
        raise SystemExit("%s should have been removed from the image" % gone)
# The physics is the part that would notice a broken numpy or sgp4 build.
from timeslides.demo import build_demo
import datetime as dt
objs = build_demo(dt.datetime(2026, 6, 24), dt.datetime(2026, 7, 1))
from timeslides.physics import compute_series, reference_satrec
ref = reference_satrec(objs, objs[0].sat_no, None)
series = compute_series(objs[0], ref, False)
assert sum(len(v) for v in series.values()) > 0, "the physics produced nothing"
print("rootfs verified: python %s, %d modules, %d offsets computed"
      % (".".join(map(str, sys.version_info[:3])),
         len(sys.modules), sum(len(v) for v in series.values())))
VERIFY

chroot "$ROOT" /opt/venv/bin/python /tmp/verify.py
rm -f "$ROOT/tmp/verify.py"

# The scanner's view of the image has to be the truth about the image. An
# empty status.d, or one that omits the C library that every binary in here
# links against, means the scan would come back clean because it could not see
# anything rather than because there is nothing to see.
if [ ! -e "$ROOT/var/lib/dpkg/status.d/libc6" ]; then
    echo "build-rootfs.sh: libc6 is in the image but not declared to the scanner" >&2
    exit 1
fi
if [ ! -e "$ROOT/etc/os-release" ] && [ ! -e "$ROOT/usr/lib/os-release" ]; then
    echo "build-rootfs.sh: no os-release, so a scanner cannot match an advisory" >&2
    exit 1
fi

# A rootfs nested inside itself means the closure copied a library by its
# in-rootfs path. It builds and runs, so nothing else would notice.
if [ -e "$ROOT$ROOT" ]; then
    echo "build-rootfs.sh: $ROOT$ROOT exists; the library closure copied into itself" >&2
    exit 1
fi
# Nor should any of the packages this image is meant not to carry come back.
# var/lib/dpkg is NOT in this list any more: status.d lives there on purpose,
# and it is metadata. What must stay out is anything that can install or run.
for unwanted in usr/bin/perl usr/bin/apt usr/bin/dpkg var/lib/dpkg/status \
                bin/login usr/bin/passwd bin/su usr/bin/su bin/sh usr/bin/sh; do
    if [ -e "$ROOT/$unwanted" ]; then
        echo "build-rootfs.sh: $unwanted is in the rootfs and should not be" >&2
        exit 1
    fi
done

echo "rootfs: $(find "$ROOT" -type f | wc -l) files, $(du -sh "$ROOT" | cut -f1)"
