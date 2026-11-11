#!/bin/bash
# Tests for the chrony configuration and the key figure publisher. Run inside
# the built image:
#   docker run --rm -v "$PWD/tests:/tests:ro" <image> bash /tests/test_app.sh
# chronyc and mosquitto_pub are stubs answering from tests/fixtures; bashio's
# Supervisor lookups are replaced by the options given to each scenario.
set -u

readonly S6=/etc/s6-overlay/s6-rc.d
# The sensor publisher; its s6 service only starts it as chronyd's user
readonly KPI_SCRIPT=/usr/local/bin/chrony-mqtt-kpi
readonly CONF=/etc/chrony/chrony.conf
WORK=$(mktemp -d)
FAILED=0

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
# at its first pause for the MQTT interval, or at pause number ROUNDS
run_service() {
    local options=$1 script=$2
    OPTIONS=${options} ROUNDS=${ROUNDS:-1} bash -c '
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
        pause() { (( --ROUNDS > 0 )) || exit 0; }
        source "$0"
    ' "${script}" > "${WORK}/log" 2>&1
}

init_config() {
    run_service "$1" "${S6}/init-chrony/run"
}

echo '# Configuration'
init_config '{"mode":"pool","ntp_pool":"time.example.net","ntp_server":[]}'
expect 'pool mode writes the pool' "$(grep -E '^(pool|server) ' "${CONF}")" 'pool time.example.net iburst'
expect 'drift kept in the data folder' "$(grep '^driftfile' "${CONF}")" 'driftfile /data/chrony/drift'
expect 'drift folder belongs to chronyd' "$(stat -c '%U %a' /data/chrony)" 'chrony 750'
expect 'steps only in the first updates' "$(grep '^makestep' "${CONF}")" 'makestep 1 3'
expect 'rate limit for clients' "$(grep '^ratelimit' "${CONF}")" 'ratelimit interval 1 burst 16'
expect 'bounded client log' "$(grep -c '^clientloglimit ' "${CONF}")" 1
expect 'no initstepslew' "$(grep -c '^initstepslew' "${CONF}")" 0
expect 'folders of the page belong to chronyd' \
    "$(stat -c '%U %a' /data/web /run/chrony-web | tr '\n' '|')" 'chrony 700|chrony 755|'
expect 'folder of the publisher belongs to its own user' "$(stat -c '%U %a' /run/chrony-mqtt)" 'chronymqtt 700'
expect 'chronyd accepts the pool configuration' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

init_config '{"mode":"server","ntp_pool":"","ntp_server":["a.example","b.example","c.example"]}'
expect 'server mode writes every server' "$(grep -c '^server .* iburst$' "${CONF}")" 3
expect 'written anew: no pool of the run before' "$(grep -c '^pool ' "${CONF}")" 0
expect 'chronyd accepts the server configuration' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

init_config '{"mode":"pool","ntp_pool":"","ntp_server":[]}'
expect 'pool mode without a pool stops the app' "$(grep -c 'mode is pool, but ntp_pool is empty' "${WORK}/log")" 1

echo '# Store of 2.1.0 and earlier'
rm -rf /data/web
printf '{"clients":{}}' > /data/clients.json
init_config '{"mode":"pool","ntp_pool":"time.example.net","ntp_server":[]}'
expect 'client store moved to /data/web' "$(cat /data/web/clients.json; [[ -e /data/clients.json ]] && echo ' left behind')" '{"clients":{}}'
expect 'moved store belongs to chronyd' "$(stat -c '%U' /data/web/clients.json)" chrony
printf '{"clients":{"old":1}}' > /data/clients.json
init_config '{"mode":"pool","ntp_pool":"time.example.net","ntp_server":[]}'
expect 'an existing store is not overwritten' "$(cat /data/web/clients.json)" '{"clients":{}}'
rm -f /data/clients.json

echo '# Services without root'
expect 'web page runs as chronyd, without the Supervisor token' \
    "$(grep -c '^exec env -u SUPERVISOR_TOKEN /command/s6-setuidgid chrony python3 ' "${S6}/web/run")" 1
expect 'chronyd without the Supervisor token' "$(grep -c '^exec env -u SUPERVISOR_TOKEN chronyd ' "${S6}/chrony/run")" 1
expect 'publisher runs as its own user' \
    "$(grep -c 's6-setuidgid chronymqtt /usr/local/bin/chrony-mqtt-kpi$' "${S6}/mqtt-kpi/run")" 1
expect 'the publisher user can start it' "$(su -s /bin/sh chronymqtt -c "test -x ${KPI_SCRIPT}"; echo $?)" 0
expect 'the publisher user is not in the group chrony' "$(id -Gn chronymqtt)" chronymqtt

echo '# Allowed networks and offline fallback'
POOL='"mode":"pool","ntp_pool":"pool.ntp.org","ntp_server":[]'
init_config "{${POOL}}"
expect 'no allowed networks: the private ones' "$(grep -E '^(allow|local)' "${CONF}" | tr '\n' '|')" \
    'allow 10.0.0.0/8|allow 172.16.0.0/12|allow 192.168.0.0/16|allow fc00::/7|allow fe80::/10|'
expect 'chronyd accepts the private networks' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0
init_config "{${POOL},\"allowed_networks\":[\"0.0.0.0/0\",\"::/0\"]}"
expect 'every client on request' "$(grep '^allow' "${CONF}" | tr '\n' '|')" 'allow 0.0.0.0/0|allow ::/0|'
expect 'chronyd accepts every network' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0
init_config "{${POOL},\"allowed_networks\":[\"10.0.0.0/16\",\"fd00::/8\"],\"serve_when_offline\":true}"
expect 'allowed networks only' "$(grep '^allow' "${CONF}" | tr '\n' '|')" 'allow 10.0.0.0/16|allow fd00::/8|'
expect 'offline fallback serves the local clock' "$(grep '^local' "${CONF}")" 'local stratum 10'
expect 'chronyd accepts networks and fallback' "$(chronyd -p -f "${CONF}" > /dev/null 2>&1; echo $?)" 0

export PATH="/tests/stubs:${PATH}" PUBLISHED="${WORK}/published" RETAINED="${WORK}/retained"
export CHRONY_WEB_STATUS="${WORK}/status.json" MQTT_CONFIG="${WORK}/mqtt"
# What the web page writes on every read of chrony; $1 true/false, $2 its age in seconds
web_status() { printf '{"at":%d,"requests_missing":%s}' "$(( $(date +%s) - ${2:-0} ))" "$1" > "${CHRONY_WEB_STATUS}"; }
: > "${RETAINED}"
last_topic() { awk -F '\t' -v t="$1" '$1 == t { v = $2 } END { print v }' "${PUBLISHED}"; }
last() { last_topic "chrony/chrony_ntp/$1"; }
KPI='{"mqtt_interval":300,"discovery_prefix":"homeassistant"}'

echo '# Problem sensors'
: > "${PUBLISHED}"
web_status false
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'no sync problem' "$(last sync_problem)" OFF
expect 'requests arrive' "$(last requests_missing)" OFF
expect 'states of the sensors before 2.0.0 cleared' \
    "$(grep -cE '^chrony/chrony_ntp/(clients|clients/attr|clients_active|ntp_requests|ntp_dropped|stratum|last_sync|system_offset|rms_offset|root_dispersion|reference)	$' "${PUBLISHED}")" 11
expect 'nothing else published' "$(cut -f1 "${PUBLISHED}" | grep '^chrony/' | sort -u | grep -cvE '/(sync_problem|requests_missing|availability|clients|clients/attr|clients_active|ntp_requests|ntp_dropped|stratum|last_sync|system_offset|rms_offset|root_dispersion|reference)$')" 0

echo '# Discovery'
DEVICE=homeassistant/device/chrony_ntp/config
dev() { last_topic "${DEVICE}"; }
cmp() { dev | jq -c --arg k "$1" '.components[$k]'; }
expect 'one message for the device' "$(grep -c "^${DEVICE}	" "${PUBLISHED}")" 1
expect 'both problem sensors in it' "$(dev | jq -c '.components | keys')" '["requests_missing","sync_problem"]'
expect 'no single entity topics' "$(grep -c '^homeassistant/[a-z_]*/chrony_ntp/[a-z_]*/config' "${PUBLISHED}")" 0
expect 'unique ID unchanged (entity IDs depend on it)' "$(cmp sync_problem | jq -r .unique_id)" chrony_ntp_sync_problem
expect 'platform per entity' "$(cmp requests_missing | jq -r '.platform + " " + .device_class')" 'binary_sensor problem'
expect 'chrony version from the image' "$(dev | jq -r '.device.model' | grep -cE '^chrony [0-9]+\.[0-9]+')" 1
expect 'availability for the whole device' "$(dev | jq -r .availability_topic)" chrony/chrony_ntp/availability
expect 'origin names the app and its support URL' "$(dev | jq -r '.origin | .name + " " + .support_url')" \
    'Chrony NTP + MQTT https://github.com/n-schilling/ha-app-chrony-mqtt'
expect 'device links to the app page' "$(dev | jq -r .device.configuration_url)" \
    "homeassistant://config/app/${HOSTNAME//-/_}/info"

echo '# Broker credentials'
expect 'broker and credentials in the option file' "$(cat "${MQTT_CONFIG}/mosquitto_pub")" \
    $'-h core-mosquitto\n-p 1883\n-u addons\n-P secret'
expect 'same for mosquitto_sub' "$(diff -q "${MQTT_CONFIG}/mosquitto_pub" "${MQTT_CONFIG}/mosquitto_sub" > /dev/null; echo $?)" 0
expect 'readable by the app user only' "$(stat -c '%a' "${MQTT_CONFIG}/mosquitto_pub" "${MQTT_CONFIG}")" $'600\n700'
expect 'no password on a command line' "$(grep -c secret "${PUBLISHED}.sub")" 0

echo '# Availability'
expect 'online with the states' "$(grep -c '^chrony/chrony_ntp/availability	online$' "${PUBLISHED}")" 1
expect 'offline on stop' "$(tail -1 "${PUBLISHED}")" 'chrony/chrony_ntp/availability	offline'
expect 'held connection with a retained last will' \
    "$(grep -c -- '--will-topic chrony/chrony_ntp/availability --will-payload offline --will-retain' "${PUBLISHED}.sub")" 1

echo '# Requests missing'
: > "${PUBLISHED}"
web_status true
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'requests missing' "$(last requests_missing)" ON
: > "${PUBLISHED}"
web_status true 300
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'stale status from the web page: unknown' "$(last requests_missing)" None
: > "${PUBLISHED}"
rm -f "${CHRONY_WEB_STATUS}"
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'no status from the web page: unknown' "$(last requests_missing)" None
web_status false

echo '# Not synchronised'
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking-unsynced.csv run_service "${KPI}" "${KPI_SCRIPT}"
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
TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'both old topics migrated' "$(grep -c '{"migrate_discovery":true}' "${PUBLISHED}")" 2
expect 'old topics cleared after the device message' \
    "$(last_topic homeassistant/sensor/chrony_ntp/stratum/config)|$(last_topic homeassistant/binary_sensor/chrony_ntp/sync_problem/config)" '|'
expect 'migration before the device message' \
    "$(awk -F '\t' -v d="${DEVICE}" '/migrate_discovery/ { m = NR } $1 == d && !f { f = NR } END { print (m < f) }' "${PUBLISHED}")" 1
expect 'gone entity removed with its platform only' \
    "$(grep "^${DEVICE}	" "${PUBLISHED}" | head -1 | cut -f2 | jq -c .components.old)" '{"platform":"sensor"}'
expect 'then left out' "$(dev | jq -c '.components | has("old")')" false
: > "${RETAINED}"

echo '# Broker away'
# Only the states fail; discovery goes through
: > "${PUBLISHED}"; rm -f "${PUBLISHED}.calls"
PUB_FAIL=999 ROUNDS=3 TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'no state published' "$(grep -c '^chrony/' "${PUBLISHED}")" 0
expect 'one warning for the whole outage, with the reason' \
    "$(grep -c 'MQTT broker not reachable (Error: Connection refused), retrying every interval' "${WORK}/log")" 1
expect 'no raw errors from the rounds' "$(grep -c '^Error: ' "${WORK}/log")" 0
expect 'a round stops at its first failed message (11 cleared old states + 3 rounds + offline on stop)' \
    "$(cat "${PUBLISHED}.calls")" 15
echo '# Broker back'
: > "${PUBLISHED}"; rm -f "${PUBLISHED}.calls"
# The 11 old states cleared at the start, then the first round fail
PUB_FAIL=12 ROUNDS=2 TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'warning while away' "$(grep -c 'MQTT broker not reachable' "${WORK}/log")" 1
expect 'back in the log' "$(grep -c 'MQTT broker answers again' "${WORK}/log")" 1
expect 'states published again' "$(last sync_problem)" OFF
rm -f "${PUBLISHED}.calls"

echo '# Broker not up for the discovery'
: > "${PUBLISHED}"; rm -f "${PUBLISHED}.dcalls"
DISC_FAIL=1 ROUNDS=2 TRACKING=/tests/fixtures/tracking.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'device announced with the next round' "$(grep -c "^${DEVICE}	" "${PUBLISHED}")" 1
expect 'announced once it went through' "$(grep -c 'Device announced' "${WORK}/log")" 1
expect 'states published in both rounds' "$(grep -c '^chrony/chrony_ntp/sync_problem	' "${PUBLISHED}")" 2
expect 'no raw errors from the discovery' "$(grep -c '^Error: ' "${WORK}/log")" 0
rm -f "${PUBLISHED}.dcalls"

echo '# Serving the local clock'
: > "${PUBLISHED}"
TRACKING=/tests/fixtures/tracking-local.csv run_service "${KPI}" "${KPI_SCRIPT}"
expect 'local clock counts as a sync problem' "$(last sync_problem)" ON

if (( FAILED )); then
    echo '--- service log:'
    cat "${WORK}/log"
    exit 1
fi
echo 'All tests passed'
