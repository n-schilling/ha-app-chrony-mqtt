# shellcheck shell=bash
# ==============================================================================
# Device based MQTT discovery: one retained message per device announces all
# its entities (components). Takes over entities that versions before
# announced one by one, and removes components that are gone.
# Needs MQ (mosquitto options) and DISCOVERY_PREFIX.
# ==============================================================================

md_pub()   { mosquitto_pub "${MQ[@]}" -r -q 1 -t "$1" -s; }
md_clear() { mosquitto_pub "${MQ[@]}" -r -q 1 -t "$1" -n; }

# Retained messages on a topic filter as "topic<TAB>payload"; waits $2 s.
# A payload may end in a newline (older versions published jq's output as it
# was), so only lines with a topic and a tab count.
md_retained() {
    { mosquitto_sub "${MQ[@]}" -t "$1" --retained-only -W "${2:-2}" -F '%t	%p' 2>/dev/null || true; } \
        | awk -F '\t' 'NF >= 2 && $1 != ""'
}

# md_announce <device ID> <payload> [<filter of the single entity topics>]
# The payload holds device, origin and components; each component has its
# platform and unique ID.
md_announce() {
    local id=$1 payload=$2 legacy=${3:-} topic="${DISCOVERY_PREFIX}/device/$1/config"
    local -a singles=()
    local t before gone

    # 1. Entities announced one by one before: Home Assistant hands them over
    #    to the device discovery, with their entity IDs and history
    if [[ -n "${legacy}" ]]; then
        mapfile -t singles < <(md_retained "${legacy}" | cut -f1)
        for t in "${singles[@]}"; do
            printf '{"migrate_discovery":true}' | md_pub "${t}"
        done
    fi

    # 2. Components of the previous announcement that are gone: first announced
    #    with their platform only, which removes them, then left out
    before=$(md_retained "${topic}" | cut -f2- | head -1)
    [[ -n "${before}" ]] || before='{}'
    gone=$(jq -c --argjson new "${payload}" '
        (.components // .cmps // {})
        | with_entries(select(.key as $k | ($new.components | has($k)) | not))
        | map_values({platform: (.platform // .p)})' <<< "${before}" 2>/dev/null) || gone=''
    if [[ -n "${gone}" && "${gone}" != '{}' ]]; then
        jq -c --argjson gone "${gone}" '.components += $gone' <<< "${payload}" | md_pub "${topic}"
    fi
    printf '%s' "${payload}" | md_pub "${topic}"

    # 3. The single entity topics go once Home Assistant has taken them over
    if (( ${#singles[@]} > 0 )); then
        sleep 2
        for t in "${singles[@]}"; do
            md_clear "${t}"
        done
        bashio::log.info "${#singles[@]} entities of ${id} moved to device discovery"
    fi
}
