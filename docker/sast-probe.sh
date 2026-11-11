#!/bin/sh
# Run the platform's SAST rules against the browser assets, locally.
#
# WHY THIS EXISTS
#
# 1.8.0 came back from the App Store with seven SAST findings in picker.js, one
# High, and nothing in the pre-flight could have shown them: ruff, eslint and
# the standards tests look for different things. The pipeline is GitLab, whose
# SAST analyser runs semgrep with GitLab's own published rules, and running
# those rules here reproduced all seven at the same lines. The page says High
# findings will start blocking releases, so this is now part of the loop.
#
# Two of the rules are worth knowing before reading a finding:
#
#   rule-node-ssrf                 taint mode; treats the first parameter of any
#                                  two-parameter function as attacker input, after
#                                  an Express (req, res) handler, and accepts it
#                                  only inside an if (allowlist.has(...)) block
#   rule-possible-timing-attacks   flags === and !== on a variable NAMED token,
#                                  secret, password and the like
#
# Needs network access to PyPI and gitlab.com. Exits non-zero on any finding.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-$(mktemp -d)}
say() { echo "sast-probe: $*"; }

python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q semgrep
git clone -q --depth 1 https://gitlab.com/gitlab-org/security-products/sast-rules.git \
    "$WORK/sast-rules"
# Semgrep reads a directory of rules flat, so give every file a unique name.
mkdir -p "$WORK/rules"
i=0
for f in $(find "$WORK/sast-rules/javascript" "$WORK/sast-rules/rules/lgpl/javascript" \
               -name '*.yml'); do
    i=$((i + 1))
    cp "$f" "$WORK/rules/r$i-$(basename "$f")"
done
say "$i JavaScript rules from GitLab's sast-rules"
cd "$ROOT"
"$WORK/venv/bin/semgrep" scan --config "$WORK/rules" --metrics=off --error \
    timeslides/report/assets/ && say "no findings"
