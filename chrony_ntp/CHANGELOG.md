# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
