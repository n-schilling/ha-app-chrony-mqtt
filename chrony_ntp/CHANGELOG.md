# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.3.0] - 2026-11-11

### Changed

- The page's header starts with the app's icon, the one of the app store, left of the name, like on the pages of all of these apps and integrations. The app serves the icon itself as `icon.png` next to the page, so it needs no new permission. The page has one view, so there are no tabs
- The page check also checks that the icon loads and sits in the row of the name

## [2.2.1] - 2026-11-11

### Changed

- The web page speaks the language of Home Assistant's interface (German or English), no longer that of the browser: it reads the user's language from Home Assistant around its frame, keeps it in a cookie for the app's ingress path and reloads once when it changes. Without that cookie it still follows the browser
- CI builds and tests aarch64 natively on an arm64 runner instead of emulating it, and no longer runs a second time on `main`: the release workflow starts on the push to `main` and checks that CI passed for that commit before it tags

## [2.2.0] - 2026-11-11

### Changed

- The web page has the header every app page shares: its name, the time of the data ("As of HH:MM:SS", now also before the first read of chrony), the refresh button and the gear for the app's configuration; the app's name and version are the page's last line

### Added

- CI check **Page**: the built image with made-up data, opened in a headless browser as Home Assistant's ingress shows it (header, settings link, version line, no console errors, light, dark and phone width); `scripts/check.sh` runs it too, and the same script takes the screenshots

## [2.1.1] - 2026-11-11

### Security

- The web page and the sensor publisher no longer run as root. The page runs as chronyd's own user (only that user and root may ask chronyd for its clients), the publisher as a user of its own that alone can read the broker's password; chronyd and the page do not get the Supervisor token
- A narrower AppArmor profile: only the capabilities, networks, programs and paths the app uses, tested on a real installation

### Changed

- The page keeps its clients in `/data/web/clients.json`; the app moves the file there from `/data` on its first start

## [2.1.0] - 2026-11-10

### Added

- The app's name and version at the bottom of the web page
- Option `log_level: debug`; the web page now follows `log_level` as well and logs once per outage when chronyc does not answer or its status for the sensors cannot be written
- chrony keeps the frequency error of the host clock in the app's data folder (`/data/chrony/drift`), so offset and skew settle soon after a restart
- chrony answers each client at most about once in two seconds on average (bursts of 16); floods from one address count as dropped requests
- Requires Home Assistant 2026.11 or later
- The base image `hassio-addons/base:21.0.11` is pinned by digest; Dependabot proposes updates of the base image

### Changed

- `allowed_networks` left empty now serves the private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`, `fe80::/10`) instead of every client; list `0.0.0.0/0` and `::/0` to serve everyone
- **NTP requests missing** only counts clients that asked in the last 7 days, so a device taken away, also one with a name, no longer holds it on for good
- The web page uses the stylesheet all of these apps share, with Home Assistant 2026.11's colours, buttons and text fields
- The page keeps at most 1000 clients; beyond that, addresses that asked only once go first, then those away longest. Clients with a name or note stay
- Reverse DNS lookups run in the background, so a slow resolver no longer holds up reading chrony or the refresh button
- chrony steps the clock only in its first three updates (`makestep 1 3`) instead of `initstepslew`, and its configuration is written anew on every start
- The broker's credentials go to the MQTT clients in a file readable by root only instead of on their command line; a broker with TLS is checked against the system's certificates
- The device in Home Assistant links to the app's page under **Settings → Apps**
- Documentation for Home Assistant 2026.11, with diagrams and new screenshots

### Fixed

- Once the connection that carries the last will broke (e.g. a restart of the broker), it was not rebuilt: the sensors stayed unavailable until the next update, up to `mqtt_interval`, and a later crash of the app no longer marked them unavailable. It now reconnects, and the app then announces the device and publishes the states at once
- The device was not announced when the broker did not take the discovery message at start (e.g. while Mosquitto was still starting); the app now tries again with every update until it does
- A client list damaged on disk was dropped without a word and overwritten; it is now kept as `clients.json.broken` and the log says so
- Saving the client list when the app stops could collide with a read of chrony running at the same time
- Right after the start, before its first sync, the page showed chrony's leap status instead of “Not synchronized”
- Connections to the web page that send nothing are closed after 10 seconds, and connections from anywhere but Home Assistant's ingress before they take up a thread

## [2.0.0] - 2026-11-02

### Added

- Name and note for each client on the web page: the pencil at the end of a row edits them; the name replaces the one from reverse DNS, the note (plain text, up to 120 characters) shows under the address, and the search finds both. Clients with a name or note stay in the list until they are forgotten
- Forget a client on the web page; it comes back with its next request, counted from then on
- Sensor **NTP requests missing**: on while no known client asked within its usual time, e.g. after a router change broke the NTP redirect; the web page shows the same in its status

### Changed

- A client counts as active while its last request is less than three of its own request intervals and an hour ago, so clients that ask every hour or once a day no longer flicker between active and inactive. The page keeps each client's interval across restarts
- The note under the client list says since when requests are counted at the earliest

### Removed

- **Breaking:** the sensors Clients (with its list of clients as attributes), Active clients, NTP requests, NTP requests dropped, Stratum, Last upstream sync, System offset, RMS offset, Root dispersion and Reference source; their values are on the web page. *Sync problem* stays. Delete their long-term statistics under **Developer tools → Statistics** if you no longer need them
- **Breaking:** the option `active_window`

### Fixed

- A client that asked for the first time right after a restart of chronyd had its requests counted twice on the web page

## [1.6.0] - 2026-11-01

### Added

- The web page counts each client's requests and drops across restarts of the app too, since the client was first seen; clients chrony forgot keep their counts in the list

### Changed

- A restart of chronyd is recognized by its process instead of by smaller counters, so requests that arrive between the restart and the next read of the page are not lost

## [1.5.0] - 2026-11-01

### Added

- The web page keeps the totals of NTP requests and dropped requests in the app's data folder, so they survive restarts; it shows them with the date counting began and chrony's own counters since the app started
- Refresh button in the header of the web page, which reads chrony at once, and the time of the shown data next to it

## [1.4.1] - 2026-11-01

### Fixed

- The web page shows the upstream servers and the reference source by name (reverse DNS) instead of their addresses

## [1.4.0] - 2026-11-01

### Added

- Web page in the sidebar (**NTP Server**, through ingress, administrators only) in Home Assistant's look and theme: sync status, upstream sources, accuracy figures and a list of all clients with name (reverse DNS), requests, drops, request interval, last request and first seen; sortable, filterable and searchable, updated every 30 seconds
- The page keeps when it first and last saw each client in the app's data folder, so clients survive restarts of the app; clients unheard of for 30 days drop off

### Changed

- The image contains Python 3 for the web page

## [1.3.6] - 2026-10-27

### Changed

- Base image `hassio-addons/base` 21.0.10 (was 21.0.8): Alpine 3.24.3, musl 1.2.6-r3, curl 8.23.0

## [1.3.5] - 2026-10-19

### Fixed

- While the MQTT broker is away, for example during its restart, the app logs one warning with the reason and one line once the broker answers again, instead of an `Error: …` line for every message of every update; an update stops at its first message the broker does not take

## [1.3.4] - 2026-10-11

### Changed

- The base image is set in the Dockerfile only (unchanged `hassio-addons/base` 21.0.8); the deprecated `build.yaml` is gone, so the Supervisor no longer warns about it when building the app

## [1.3.3] - 2026-10-09

### Changed

- Base image `hassio-addons/base` 21.0.8 (was 21.0.4): Alpine 3.24.2, OpenSSL 3.5.9, tzdata 2026e

## [1.3.2] - 2026-10-09

### Changed

- s6-overlay logs only warnings and errors, so the start and stop of the app no longer fill the log with `s6-rc: info` lines

## [1.3.1] - 2026-10-07

### Fixed

- Moving to device discovery stopped on the empty lines between old discovery messages that end in a newline

## 1.3.0 - 2026-10-07

### Changed

- Device based MQTT discovery: one retained message announces the device with all its entities. The entities announced one by one before are handed over with their entity IDs and history, and their old topics are removed
- `log_level` offers `info`, `warning` or `error` only

## 1.2.0 - 2026-10-07

### Added

- Option `allowed_networks`: only these networks may take time from the server
- Option `serve_when_offline`: keeps serving the host clock at stratum 10 without upstream servers; the sync problem sensor shows it
- Entities turn unavailable when the app stops or dies (MQTT availability with a last will)
- Discovery names the app, its version and support URL (origin); the device links to the app page
- Icon, logo, German translation of the options
- Documentation: example automations, troubleshooting, known limitations, removing the app

### Changed

- A failing chronyc query is logged once per outage instead of on every interval
- chrony pinned to 4.8, no longer to one package revision, so the build survives Alpine updates

### Security

- NTP pool and server names are checked against a host name pattern
- Fewer rights: no Supervisor API access; AppArmor profile

## 1.1.1 - 2026-10-06

### Added

- Tests and CI

### Changed

- Unused fields of chronyc's output are no longer kept in variables (ShellCheck); no change in behaviour

## 1.1.0 - 2026-10-06

### Added

- chrony version detected for the device info
- Documentation

### Changed

- English texts throughout
- Generic pool.ntp.org defaults

### Fixed

- The first update waits for chrony to synchronize, so a start no longer reports a sync problem that is none

## 1.0.2 - 2026-10-06

### Added

- First version in this repository

[1.3.1]: https://github.com/n-schilling/ha-app-chrony-mqtt/releases/tag/v1.3.1
