#!/bin/bash
# Runs locally what the CI workflow runs, so a push only confirms what already
# passed here: the changelog check, shellcheck, ruff, the image build, the
# tests of the configuration and the publisher, the tests of the web page and
# the tests against a real Mosquitto broker. Keep the steps in step with
# .github/workflows/ci.yaml.
#
# Usage: scripts/check.sh [--no-broker]
#
# Needs Docker: the tests run inside the built image, as in CI, for the
# architecture of this machine (CI builds and tests amd64 and aarch64).
# The linters (shellcheck, ruff) come from PATH, else from a container image or uvx.
# Only the app configuration linter (frenck/action-addon-linter) runs in CI
# alone.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

readonly IMAGE=chrony-mqtt:check
readonly NET=chrony-mqtt-check
readonly BROKER=chrony-mqtt-check-broker
# Keep in step with ci.yaml
readonly RUFF_VERSION=0.17.3
readonly SHELLCHECK_IMAGE=koalaman/shellcheck:v0.11.0
readonly MOSQUITTO_IMAGE=eclipse-mosquitto:2.1.2-alpine

no_broker=false
for arg in "$@"; do
    case "$arg" in
        --no-broker) no_broker=true ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

if ! docker info > /dev/null 2>&1; then
    echo "Docker is not running; the tests need it." >&2
    exit 2
fi

failed=()
skipped=()

run() { # run <name> <command...>
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok: $name"
    else
        echo "    FAILED: $name"
        failed+=("$name")
    fi
    echo
}

version=$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' chrony_ntp/config.yaml)

shell_scripts=(
    chrony_ntp/rootfs/etc/s6-overlay/s6-rc.d/*/run
    chrony_ntp/rootfs/usr/local/lib/mqtt-device.sh
    chrony_ntp/rootfs/etc/s6-overlay/s6-rc.d/chrony/finish
    scripts/check.sh
    tests/test_app.sh tests/test_broker.sh tests/stubs/chronyc tests/stubs/mosquitto_*
)
shellcheck_run() {
    if command -v shellcheck > /dev/null; then
        shellcheck --shell=bash --severity=warning --external-sources "${shell_scripts[@]}"
    else
        docker run --rm -v "$PWD":/mnt:ro -w /mnt "$SHELLCHECK_IMAGE" \
            --shell=bash --severity=warning --external-sources "${shell_scripts[@]}"
    fi
}
ruff_run() {
    if command -v ruff > /dev/null && [[ "$(ruff --version)" == "ruff $RUFF_VERSION" ]]; then
        ruff check chrony_ntp/rootfs/usr/local/lib tests
    else
        uvx "ruff==$RUFF_VERSION" check chrony_ntp/rootfs/usr/local/lib tests
    fi
}
in_image() { # in_image <docker run options...> -- <entrypoint> <arguments...>
    local -a options=()
    while [[ $1 != -- ]]; do
        options+=("$1")
        shift
    done
    shift
    docker run --rm "${options[@]}" -v "$PWD/tests:/tests:ro" --entrypoint "$1" "$IMAGE" "${@:2}"
}
broker_tests() {
    docker rm -f "$BROKER" > /dev/null 2>&1
    docker network rm "$NET" > /dev/null 2>&1
    docker network create "$NET" > /dev/null || return 1
    docker run -d --name "$BROKER" --network "$NET" --network-alias broker \
        -v "$PWD/tests/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" "$MOSQUITTO_IMAGE" > /dev/null || return 1
    in_image --network "$NET" --add-host supervisor:127.0.0.1 -- bash /tests/test_broker.sh
    local result=$?
    ((result)) && docker logs "$BROKER"
    docker rm -f "$BROKER" > /dev/null
    docker network rm "$NET" > /dev/null
    return "$result"
}

run "Changelog has the version $version" bash -c \
    '.github/scripts/changelog-section.sh chrony_ntp/CHANGELOG.md "$1" > /dev/null' _ "$version"
run "Shell scripts" shellcheck_run
run "Python" ruff_run
run "Build" docker build -q --build-arg "BUILD_VERSION=$version" -t "$IMAGE" chrony_ntp
if [[ " ${failed[*]-} " == *" Build "* ]]; then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
run "Test" in_image --network none -- bash /tests/test_app.sh
run "Test the web page" in_image --network none -- python3 -m unittest discover -s /tests -p 'test_*.py'
if $no_broker; then
    skipped+=("Against a real MQTT broker (--no-broker)")
else
    run "Against a real MQTT broker" broker_tests
fi

echo "===================="
# The ${arr[@]+"${arr[@]}"} form keeps an empty array from tripping `set -u` in the bash 3.2 of macOS
for name in ${skipped[@]+"${skipped[@]}"}; do
    echo "skipped: $name"
done
if ((${#failed[@]})); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
