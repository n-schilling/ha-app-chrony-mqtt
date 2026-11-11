#!/bin/bash
# Tests the key figure publisher against a real Mosquitto broker: device
# discovery, retained states, the hand-over of single entity topics, the stop
# and the last will. Run inside the built image, next to a broker reachable as
# "broker" (see the CI workflow):
#   docker run --rm --network <net> -v "$PWD/tests:/tests:ro" <image> bash /tests/test_broker.sh
# chronyc is a stub; mosquitto_pub and mosquitto_sub are the real ones.
set -u

readonly RUN=/usr/local/bin/chrony-mqtt-kpi
readonly DEVICE=homeassistant/device/chrony_ntp/config
readonly AVAIL=chrony/chrony_ntp/availability
WORK=$(mktemp -d)
FAILED=0
export TRACKING=/tests/fixtures/tracking.csv

expect() {
    local name=$1 got=$2 want=$3
    if [[ "${got}" == "${want}" ]]; then
        echo "ok    ${name}"
    else
        echo "FAIL  ${name}: got '${got}', want '${want}'"
        FAILED=1
    fi
}

# Retained payload of a topic, empty when there is none
retained() { mosquitto_sub -h broker -t "$1" --retained-only -W 2 -C 1 2>/dev/null; }
# Kills every process of the service at once, as when the container dies:
# the service, its availability connection and that connection's loop
kill_all() {
    local p cmd
    for p in /proc/[0-9]*; do
        cmd=$(tr '\0' ' ' < "${p}/cmdline" 2>/dev/null) || continue
        if [[ "${cmd}" == *"${RUN}"* || "${cmd}" == mosquitto_sub*-availability* ]]; then
            kill -9 "${p#/proc/}" 2>/dev/null
        fi
    done
}
# Waits up to $2 s for a command to succeed
wait_for() { local i; for ((i = 0; i < $2 * 5; i++)); do eval "$1" && return 0; sleep 0.2; done; return 1; }

# Starts the publisher in the background with the stub chronyc
start_service() {
    PATH="/tests/stubs/chronyc-only:${PATH}" bash -c '
        source /usr/lib/bashio/bashio.sh
        bashio::config() {
            case "$1" in
                mqtt_interval) echo "${MQTT_INTERVAL:-1}" ;;
                discovery_prefix) echo homeassistant ;;
                *) echo null ;;
            esac
        }
        bashio::services.available() { return 0; }
        bashio::services() {
            case "$2" in
                host) echo broker ;;
                port) echo 1883 ;;
                *) echo "" ;;
            esac
        }
        source "$0"
    ' "${RUN}" >> "${WORK}/log" 2>&1 &
    SERVICE=$!
}

echo '# Hand-over from single entity discovery'
# As versions before 1.3.0 left them: jq output with its trailing newline
for e in stratum sync_problem; do
    jq -nc --arg u "chrony_ntp_${e}" '{unique_id:$u}' \
        | mosquitto_pub -h broker -r -t "homeassistant/sensor/chrony_ntp/${e}/config" -s
done
# A state of a sensor that moved to the web page in 2.0.0
printf 2 | mosquitto_pub -h broker -r -t chrony/chrony_ntp/stratum -s
start_service
wait_for '[[ -n "$(retained chrony/chrony_ntp/sync_problem)" ]]' 90
expect 'device message retained' "$(retained "${DEVICE}" | jq -c '.components | keys')" '["requests_missing","sync_problem"]'
expect 'old topics removed' "$(retained homeassistant/sensor/chrony_ntp/stratum/config)$(retained homeassistant/sensor/chrony_ntp/sync_problem/config)" ''
expect 'hand-over logged' "$(grep -c '2 entities of chrony_ntp moved to device discovery' "${WORK}/log")" 1
expect 'state retained' "$(retained chrony/chrony_ntp/sync_problem)" OFF
expect 'unknown without the web page' "$(retained chrony/chrony_ntp/requests_missing)" None
expect 'state of a gone sensor cleared' "$(retained chrony/chrony_ntp/stratum)" ''
expect 'online' "$(retained "${AVAIL}")" online

echo '# Regular stop'
kill -TERM "${SERVICE}"
wait "${SERVICE}"
expect 'offline after a stop' "$(retained "${AVAIL}")" offline

echo '# Last will'
start_service
wait_for '[[ "$(retained "${AVAIL}")" == online ]]' 90
# The app dies without saying goodbye: the broker sends the last will
kill_all
wait_for '[[ "$(retained "${AVAIL}")" == offline ]]' 10
expect 'offline through the last will' "$(retained "${AVAIL}")" offline
expect 'second start found nothing to hand over' "$(grep -c 'moved to device discovery' "${WORK}/log")" 1

echo '# Reconnect of the availability connection'
# A long interval: only the reconnect can bring "online" and the device back soon
MQTT_INTERVAL=3600 start_service
wait_for '[[ "$(retained "${AVAIL}")" == online ]]' 90
# The broker lost the device message, and the held connection breaks
mosquitto_pub -h broker -r -t "${DEVICE}" -n
for p in /proc/[0-9]*; do
    [[ "$(tr '\0' ' ' < "${p}/cmdline" 2>/dev/null)" == mosquitto_sub*-availability* ]] && kill -9 "${p#/proc/}"
done
wait_for '[[ "$(retained "${AVAIL}")" == offline ]]' 10
expect 'last will after the break' "$(retained "${AVAIL}")" offline
wait_for '[[ "$(retained "${AVAIL}")" == online ]]' 30
expect 'online again after the reconnect' "$(retained "${AVAIL}")" online
expect 'device announced again' "$(retained "${DEVICE}" | jq -c '.components | keys')" '["requests_missing","sync_problem"]'
kill -TERM "${SERVICE}"
wait "${SERVICE}"

if (( FAILED )); then
    echo '--- service log:'
    cat "${WORK}/log"
    exit 1
fi
echo 'All tests passed'
