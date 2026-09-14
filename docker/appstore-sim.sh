#!/bin/sh
# Run the suite the way the App Store's test stage runs it.
#
# WHY THIS EXISTS
#
# Three pipeline failures in a row were environment differences, not code
# defects. Every one of them passed locally and every one was cheap to
# reproduce once the difference was named:
#
#   ● the tree is UNPACKED, not cloned, so there is no .git;
#   ● sonar-project.properties is NOT in it, because the ingest does not carry
#     that file;
#   ● GIT IS NOT INSTALLED. The job container is python:3.12-slim and the
#     checkout is done by a different container, so .git can be present with
#     no git to read it. A helper that shelled out to git raised
#     FileNotFoundError inside a skipif decorator, which is evaluated at
#     import, so pytest reported a collection error and abandoned the run.
#     684 passing tests never executed.
#   ● Playwright is not installed, because it is not in requirements.txt. The
#     browser suite module-skips, which is correct and should stay visible.
#
# So the difference is reproduced rather than remembered. Run this before
# every upload; it takes about as long as the suite does.
#
#     sh docker/appstore-sim.sh
#
# It builds the tree from `git ls-files`, which is what the upload zip
# contains, so it also catches a file that is present locally and untracked.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-$(mktemp -d)}
PY=${PY:-"$ROOT/.venv/bin/python"}

echo "assembling the App Store's view of this repository in $WORK"
mkdir -p "$WORK"
( cd "$ROOT" && git ls-files -z | tar -cf - --null -T - ) | tar -xf - -C "$WORK"

# What the ingest does not carry.
rm -f "$WORK/sonar-project.properties"
rm -rf "$WORK/.git"

# A PATH with everything on it EXCEPT git.
#
# Not an empty PATH. The first version of this script used one, and twelve
# tests failed because docker/harden.sh needs find, chmod and rm: that is this
# script breaking the suite, not the platform. The job container is a normal
# Debian userland that happens not to have git installed, so the simulation
# mirrors exactly that, by linking every executable on PATH except git itself.
NOGIT="$WORK/.nogit-path"
mkdir -p "$NOGIT"
echo "$PATH" | tr ':' '\n' | while read -r dir; do
    [ -d "$dir" ] || continue
    for exe in "$dir"/*; do
        [ -x "$exe" ] && [ ! -d "$exe" ] || continue
        name=$(basename "$exe")
        case "$name" in git|git-*) continue ;; esac
        [ -e "$NOGIT/$name" ] || ln -s "$exe" "$NOGIT/$name" 2>/dev/null || true
    done
done
if PATH="$NOGIT" command -v git >/dev/null 2>&1; then
    echo "appstore-sim.sh: git is still reachable, so this proves nothing" >&2
    exit 1
fi

echo "running the suite as the platform runs it"
cd "$WORK"
PATH="$NOGIT" "$PY" -m pytest --cov --cov-report=xml:coverage.xml
status=$?

# The report is the artefact the gate reads, so its shape is checked here too.
PATH="$NOGIT" "$PY" - <<'CHECK'
import sys
import xml.etree.ElementTree as ET
root = ET.parse("coverage.xml").getroot()
names = [c.get("filename") for c in root.iter("class")]
roots = [(s.text or "") for s in root.iter("source")]
assert not any(r.startswith("/") for r in roots), f"absolute source root: {roots}"
assert names and all(n.startswith("timeslides/") for n in names), names[:3]
print(f"coverage report: line-rate {root.get('line-rate')}, "
      f"{root.get('lines-covered')} of {root.get('lines-valid')} lines, "
      f"{len(names)} files, all paths relative")
CHECK

echo "App Store simulation passed"
exit $status
