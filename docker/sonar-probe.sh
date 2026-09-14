#!/bin/sh
# Run a real SonarQube against this repository and read the coverage back.
#
# WHY THIS EXISTS
#
# The gate reported "Line coverage is 0.0%" four uploads running, against a
# suite at 100 per cent. Three rounds of reasoning about why produced three
# plausible answers and three wrong fixes. This script replaces the reasoning
# with the measurement: it starts SonarQube, scans the project three ways, and
# prints what each one scores.
#
#   with our sonar-project.properties      expect 100.0%
#   without it (the App Store's tree)      expect a number, not zero, because
#                                          the plugin's default pattern
#                                          coverage-reports/*coverage-*.xml
#                                          finds the copy the suite writes
#   with no coverage report present        expect 0.0%
#
# The third is the one that matters. 0.0% is not a low score: it is the score
# a project gets when the scanner finds no report at all, and it is what the
# pipeline reports. The scanner says so in as many words:
#
#   WARN  No report was found for sonar.python.coverage.reportPaths
#         using pattern coverage.xml
#
#     sh docker/sonar-probe.sh
#
# Needs a Docker daemon and about 2 GB of memory. Takes roughly five minutes.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-$(mktemp -d)}
SQ=${SQ:-mirror.gcr.io/library/sonarqube:community}
SCANNER=${SCANNER:-mirror.gcr.io/sonarsource/sonar-scanner-cli:latest}
PORT=${PORT:-9000}
URL="http://127.0.0.1:$PORT"

say() { echo "sonar-probe: $*"; }

docker rm -f sq-probe >/dev/null 2>&1 || true
say "starting SonarQube (this takes a minute or two)"
docker run -d --name sq-probe -p "$PORT:9000" \
    -e SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true "$SQ" >/dev/null

# Elasticsearch measures free space against the whole device, so a sandbox with
# a quota looks full to it and it then refuses to allocate the shards SonarQube
# needs to start. Disabling the threshold is about this environment, not about
# the product.
i=0
while [ "$i" -lt 60 ]; do
    i=$((i + 1))
    if docker exec sq-probe sh -c 'curl -s -XPUT localhost:9001/_cluster/settings \
        -H "Content-Type: application/json" \
        -d "{\"persistent\":{\"cluster.routing.allocation.disk.threshold_enabled\":false}}"' \
        2>/dev/null | grep -q '"acknowledged":true'; then
        say "disk watermark disabled"
        break
    fi
    sleep 2
done

i=0
while [ "$i" -lt 90 ]; do
    i=$((i + 1))
    if curl -s --noproxy '*' "$URL/api/system/status" 2>/dev/null | grep -q '"status":"UP"'; then
        say "SonarQube is up"
        break
    fi
    sleep 3
done
curl -s --noproxy '*' "$URL/api/system/status" | grep -q '"status":"UP"' || {
    say "SonarQube did not start; see: docker logs sq-probe" >&2
    exit 1
}

curl -s --noproxy '*' -u admin:admin -X POST \
    "$URL/api/users/change_password?login=admin&previousPassword=admin&password=Timeslides123!" \
    >/dev/null
TOKEN=$(curl -s --noproxy '*' -u "admin:Timeslides123!" -X POST \
    "$URL/api/user_tokens/generate?name=probe-$$" |
    sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || { say "could not obtain a token" >&2; exit 1; }

# The tree as uploaded, plus the reports the suite writes at run time.
mkdir -p "$WORK/tree"
( cd "$ROOT" && git ls-files -z | tar -cf - --null -T - ) | tar -xf - -C "$WORK/tree"
cp "$ROOT/coverage.xml" "$WORK/tree/" 2>/dev/null || true
cp -r "$ROOT/coverage-reports" "$WORK/tree/" 2>/dev/null || true

scan() {
    key=$1
    dir=$2
    shift 2
    docker run --rm --network=host \
        -e SONAR_HOST_URL="$URL" -e SONAR_TOKEN="$TOKEN" \
        -v "$dir:/usr/src" "$SCANNER" -Dsonar.projectKey="$key" "$@" \
        > "$WORK/$key.log" 2>&1 || true
    grep -iE "Parsing report|No report was found" "$WORK/$key.log" | sed 's/^/    /' || true
    i=0
    while [ "$i" -lt 30 ]; do
        i=$((i + 1))
        curl -s --noproxy '*' -u "$TOKEN:" \
            "$URL/api/ce/component?component=$key" | grep -q '"queue":\[\]' && break
        sleep 2
    done
    printf '    coverage: '
    curl -s --noproxy '*' -u "$TOKEN:" \
        "$URL/api/measures/component?component=$key&metricKeys=coverage" |
        sed -n 's/.*"value":"\([^"]*\)".*/\1/p'
}

say "1. as this repository is configured"
scan probe-with-config "$WORK/tree"

say "2. without sonar-project.properties, which the App Store tree does not carry"
cp -r "$WORK/tree" "$WORK/noconfig"
rm -f "$WORK/noconfig/sonar-project.properties"
scan probe-no-config "$WORK/noconfig" -Dsonar.sources=.

say "3. with the pipeline's own flags, which override the properties file"
say "   (-Dsonar.sources=app.py,timeslides -Dsonar.python.coverage.reportPaths=coverage.xml)"
scan probe-platform-flags "$WORK/tree" \
    -Dsonar.sources=app.py,timeslides -Dsonar.tests=tests \
    -Dsonar.python.coverage.reportPaths=coverage.xml

say "4. the same flags with no properties file, so no coverage exclusions"
cp -r "$WORK/tree" "$WORK/flagsonly"
rm -f "$WORK/flagsonly/sonar-project.properties"
scan probe-flags-only "$WORK/flagsonly" \
    -Dsonar.sources=app.py,timeslides -Dsonar.tests=tests \
    -Dsonar.python.coverage.reportPaths=coverage.xml

say "5. with no coverage report present, which is what 0.0% means"
cp -r "$WORK/tree" "$WORK/noreport"
rm -f "$WORK/noreport/coverage.xml"
rm -rf "$WORK/noreport/coverage-reports"
scan probe-no-report "$WORK/noreport"

say "done. docker rm -f sq-probe when finished."
