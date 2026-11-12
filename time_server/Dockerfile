# Multi-arch image, pinned by version and digest, so a moved tag cannot change
# it unnoticed; Dependabot proposes updates of both. Without a build.yaml the
# Supervisor passes no BUILD_FROM. Both stages name the same image.

# chrony is built from its release: Alpine's stable branch is a version behind.
# The tarball is pinned by its SHA-512, the same as in Alpine's own build
# (aports main/chrony); Dependabot does not see it, so check new releases at
# https://chrony-project.org/news.html by hand. Built like Alpine's package
# without NTS: the same features as before (+CMDMON +REFCLOCK +RTC +PRIVDROP
# +SCFILTER -NTS -SECHASH +IPV6)
FROM ghcr.io/hassio-addons/base:21.0.11@sha256:79fd58d9d7f2820e8b2e7d2b11a24e22b96ae7a48569af3372183592a1b22974 AS chrony
ARG CHRONY_VERSION=4.9
ARG CHRONY_SHA512=9eeee9738727e86562d75e4aee60916957e0626b767d9fea325918d3973e02b3f252c32fd1c4b4cf8461406acccbc8196d6f7d2e9d2d3ce205bc3245b83924b5
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /build
RUN \
    apk add --no-cache build-base bison libcap-dev libseccomp-dev \
    && curl -fsSL -o chrony.tar.gz "https://chrony-project.org/releases/chrony-${CHRONY_VERSION}.tar.gz" \
    && echo "${CHRONY_SHA512}  chrony.tar.gz" | sha512sum -c - \
    && tar -xzf chrony.tar.gz --strip-components=1 \
    && ./configure \
        --prefix=/usr \
        --mandir=/usr/share/man \
        --sysconfdir=/etc/chrony \
        --chronyrundir=/run/chrony \
        --disable-readline \
        --disable-nts \
        --disable-sechash \
        --with-ntp-era=0 \
        --with-user=chrony \
        --with-chronyc-user=chrony \
        --with-pidfile=/run/chrony/chronyd.pid \
        --enable-scfilter \
    && make -j"$(nproc)" \
    && make DESTDIR=/out install \
    && rm -rf /out/usr/share/man

FROM ghcr.io/hassio-addons/base:21.0.11@sha256:79fd58d9d7f2820e8b2e7d2b11a24e22b96ae7a48569af3372183592a1b22974

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# chronyd and the page run as user chrony, the HTTP time service as timehttp,
# the sensor publisher as chronymqtt
RUN \
    apk add --no-cache \
        libcap2 \
        libseccomp \
        mosquitto-clients \
        python3 \
    && addgroup -S chrony \
    && adduser -S -D -H -G chrony -h /var/lib/chrony -s /sbin/nologin chrony \
    && addgroup -S timehttp \
    && adduser -S -D -H -G timehttp -h /nonexistent -s /sbin/nologin timehttp \
    && addgroup -S chronymqtt \
    && adduser -S -D -H -G chronymqtt -h /nonexistent -s /sbin/nologin chronymqtt \
    && install -d -o chrony -g chrony -m 0750 /var/lib/chrony \
    && install -d -m 0755 /etc/chrony

COPY --from=chrony /out/usr/ /usr/
COPY rootfs /
# The store icon, also at the left of the page's header (served as icon.png)
COPY icon.png /usr/local/share/ha-app/icon.png

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
