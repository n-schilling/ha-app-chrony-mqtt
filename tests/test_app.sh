#!/bin/bash
# Tests for the chrony configuration and the key figure publisher. Run inside
# the built image:
#   docker run --rm -v "$PWD/tests:/tests:ro" <image> bash /tests/test_app.sh
# chronyc and mosquitto_pub are stubs answering from tests/fixtures; bashio's
# Supervisor lookups are replaced by the options given to each scenario.
set -u

readonly S6=/etc/s6-overlay/s6-rc.d
readonly CONF=/etc/chrony/chrony.conf
WORK=$(mktemp -d)
FAILED=0
cp "${CONF}" "${WORK}/chrony.conf.orig"

expect() {
    local name=$1 got=$2 want=$3
    if [[ "${got}" == "${want}" ]]; then
        echo "ok    ${name}"
    else
        echo "FAIL  ${name}: got '${got}', want '${want}'"
        FAILED=1
    fi
}

# Runs one service script with the options in $1 (JSON); the main loop ends
# at its first pause for the MQTT interval
run_service() {
    local options=$1 script=$2
    OPTIONS=${options} bash -c '
        source /usr/lib/bashio/bashio.sh
        bashio::config() {
            jq -r --arg k "$1" ".[\$k] | if type == \"array\" then .[] elif . == null then \"null\" else . end" <<< "${OPTIONS}"
        }
        bashio::services.available() { return 0; }
        bashio::services() {
            case "$2" in
                host) echo core-mosquitto ;;
                port) echo 1883 ;;
                username) echo addons ;;
                password) echo secret ;;
            esac
        }
        sleep() { return 0; }
        pause() { exit 0; }
        source "$0"
    ' "${script}" > "${WORK}/log" 2>&1
}

init_config() {
    cp "${WORK}/chrony.conf.orig" "${CONF}"
    run_service "$1" "${S6}/init-chrony/run"
}

echo '# Configuration'
init_config '{"mode":"pool","ntp_pool":"ptbtime1.ptb.de","ntp_server":[]}'
expect 'pool mode writes the pool' "$(grep -E '^(pool|server) ' "${CONF}")" 'pool ptbtime1.ptb.de iburst'
expect 'pool mode steps the clock from it' "$(grep '^initstepslew' "${CONF}")" 'initstepslew 10 ptbtime1.ptb.de'
expect 'chronyd accepts the pool configuration' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

init_config '{"mode":"server","ntp_pool":"","ntp_server":["a.example","b.example","c.example"]}'
expect 'server mode writes every server' "$(grep -c '^server .* iburst$' "${CONF}")" 3
expect 'server mode steps the clock from all of them' "$(grep '^initstepslew' "${CONF}")" \
    'initstepslew 10 a.example b.example c.example'
expect 'chronyd accepts the server configuration' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

init_config '{"mode":"pool","ntp_pool":"","ntp_server":[]}'
expect 'pool mode without a pool stops the app' "$(grep -c 'mode is pool, but ntp_pool is empty' "${WORK}/log")" 1

echo '# Allowed networks and offline fallback'
POOL='"mode":"pool","ntp_pool":"pool.ntp.org","ntp_server":[]'
init_config "{${POOL}}"
expect 'no allowed networks: every client' "$(grep -E '^(allow|local)' "${CONF}")" 'allow all'
init_config "{${POOL},\"allowed_networks\":[\"10.0.0.0/16\",\"fd00::/8\"],\"serve_when_offline\":true}"
expect 'allowed networks only' "$(grep '^allow' "${CONF}" | tr '\n' '|')" 'allow 10.0.0.0/16|allow fd00::/8|'
expect 'offline fallback serves the local clock' "$(grep '^local' "${CONF}")" 'local stratum 10'
expect 'chronyd accepts networks and fallback' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

export PATH="/tests/stubs:${PATH}" PUBLISHED="${WORK}/published" RETAINED="${WORK}/retained"
: > "${RETAINED}"
last_topic() { awk -F '\t' -v t="$1" '$1 == t { v = $2 } END { print v }' "${PUBLISHED}"; }
last() { last_topic "chrony/chrony_ntp/$1"; }
KPI='{"mqtt_interval":300,"active_window":3900,"discovery_prefix":"homeassistant"}'

echo '# Key figures'
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${S6}/mqtt-kpi/run"
expect 'clients without localhost' "$(last clients)" 3
expect 'active clients within the window, "never" excluded' "$(last clients_active)" 1
expect 'NTP requests' "$(last ntp_requests)" 62
expect 'NTP requests dropped' "$(last ntp_dropped)" 0
expect 'stratum' "$(last stratum)" 2
expect 'system offset in ms' "$(last system_offset)" 0.0911
expect 'RMS offset in ms' "$(last rms_offset)" 0.0685
expect 'root dispersion in ms' "$(last root_dispersion)" 2.4638
expect 'reference source by name' "$(last reference)" 192.53.103.108
expect 'last upstream sync as epoch seconds' "$(last last_sync)" 1788640381
expect 'no sync problem' "$(last sync_problem)" OFF
expect 'client attributes' "$(last clients/attr | jq -c '[.active_window_s, (.clients | map(.ip + ":" + (.last_seen | tostring)))]')" \
    '[3900,["192.0.2.6:120","192.0.2.7:5000","192.0.2.8:4294967295"]]'

echo '# Discovery'
DEVICE=homeassistant/device/chrony_ntp/config
dev() { last_topic "${DEVICE}"; }
cmp() { dev | jq -c --arg k "$1" '.components[$k]'; }
expect 'one message for the device' "$(grep -c "^${DEVICE}	" "${PUBLISHED}")" 1
expect 'every entity in it' "$(dev | jq '.components | length')" 11
expect 'no single entity topics' "$(grep -c '^homeassistant/[a-z_]*/chrony_ntp/[a-z_]*/config' "${PUBLISHED}")" 0
expect 'unique ID unchanged (entity IDs depend on it)' "$(cmp stratum | jq -r .unique_id)" chrony_ntp_stratum
expect 'platform per entity' "$(cmp sync_problem | jq -r '.platform + " " + .device_class')" 'binary_sensor problem'
expect 'chrony version from the image' "$(dev | jq -r '.device.model' | grep -cE '^chrony [0-9]+\.[0-9]+')" 1
expect 'availability for the whole device' "$(dev | jq -r .availability_topic)" chrony/chrony_ntp/availability
expect 'origin names the app and its support URL' "$(dev | jq -r '.origin | .name + " " + .support_url')" \
    'Chrony NTP + MQTT https://github.com/n-schilling/ha-app-chrony-mqtt'
expect 'device links to the app page' "$(dev | jq -r .device.configuration_url)" \
    "homeassistant://hassio/addon/${HOSTNAME//-/_}/info"

echo '# Availability'
expect 'online with the states' "$(grep -c '^chrony/chrony_ntp/availability	online$' "${PUBLISHED}")" 1
expect 'offline on stop' "$(tail -1 "${PUBLISHED}")" 'chrony/chrony_ntp/availability	offline'
expect 'held connection with a retained last will' \
    "$(grep -c -- '--will-topic chrony/chrony_ntp/availability --will-payload offline --will-retain' "${PUBLISHED}.sub")" 1

echo '# Not synchronised'
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking-unsynced.csv run_service "${KPI}" "${S6}/mqtt-kpi/run"
expect 'waits for a sync, then publishes anyway' "$(grep -c 'not synchronized after two minutes' "${WORK}/log")" 1
expect 'sync problem at stratum 16' "$(last sync_problem)" ON

echo '# Moving from single entity discovery'
# What versions before 1.3.0 left on the broker: one topic per entity, and a
# device announcement with an entity that is gone
printf '%s\t%s\n' \
    homeassistant/sensor/chrony_ntp/stratum/config '{"unique_id":"chrony_ntp_stratum"}' \
    homeassistant/binary_sensor/chrony_ntp/sync_problem/config '{"unique_id":"chrony_ntp_sync_problem"}' \
    homeassistant/device/chrony_ntp/config '{"components":{"stratum":{"platform":"sensor"},"old":{"platform":"sensor","unique_id":"x"}}}' \
    > "${RETAINED}"
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${S6}/mqtt-kpi/run"
expect 'both old topics migrated' "$(grep -c '{"migrate_discovery":true}' "${PUBLISHED}")" 2
expect 'old topics cleared after the device message' \
    "$(last_topic homeassistant/sensor/chrony_ntp/stratum/config)|$(last_topic homeassistant/binary_sensor/chrony_ntp/sync_problem/config)" '|'
expect 'migration before the device message' \
    "$(awk -F '\t' -v d="${DEVICE}" '/migrate_discovery/ { m = NR } $1 == d && !f { f = NR } END { print (m < f) }' "${PUBLISHED}")" 1
expect 'gone entity removed with its platform only' \
    "$(grep "^${DEVICE}	" "${PUBLISHED}" | head -1 | cut -f2 | jq -c .components.old)" '{"platform":"sensor"}'
expect 'then left out' "$(dev | jq -c '.components | has("old")')" false
: > "${RETAINED}"

echo '# Serving the local clock'
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking-local.csv run_service "${KPI}" "${S6}/mqtt-kpi/run"
expect 'local clock counts as a sync problem' "$(last sync_problem)" ON

if (( FAILED )); then
    echo '--- service log:'
    cat "${WORK}/log"
    exit 1
fi
echo 'All tests passed'
