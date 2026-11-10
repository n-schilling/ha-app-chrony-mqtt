# Multi-arch image; without a build.yaml the Supervisor passes no BUILD_FROM.
# Dependabot keeps the tag current.
FROM ghcr.io/hassio-addons/base:21.0.11

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN \
    apk add --no-cache \
        chrony~4.8 \
        mosquitto-clients \
        python3

COPY rootfs /

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION
# The version from config.yaml, which the Supervisor (and CI) pass at build
# time: shown at the bottom of the web page and as origin of the MQTT discovery
ENV APP_VERSION=${BUILD_VERSION}
# s6-overlay: warnings and errors only, no per-service start/stop info lines
ENV S6_VERBOSITY=1
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION}