# Chrony NTP + MQTT app for Home Assistant

[![Release](https://img.shields.io/github/v/release/n-schilling/ha-app-chrony-mqtt)](https://github.com/n-schilling/ha-app-chrony-mqtt/releases)
[![CI](https://github.com/n-schilling/ha-app-chrony-mqtt/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/n-schilling/ha-app-chrony-mqtt/actions/workflows/ci.yaml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5?logo=homeassistant&logoColor=white)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

![Chrony NTP + MQTT](chrony_ntp/logo.png)

A Home Assistant app (formerly add-on) that runs [chrony](https://chrony-project.org/) as a local NTP server for your network and publishes its key figures (clients, requests, offsets, sync state) as sensors through MQTT discovery.

## Installation

1. Add the repository to your Home Assistant instance:

   [![Add the repository to My Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fn-schilling%2Fha-app-chrony-mqtt)

   Or add it manually: **Settings → Apps → Install app → ⋮ → Repositories** (before Home Assistant 2026.2: **Settings → Add-ons → Add-on store**), then paste:

   ```text
   https://github.com/n-schilling/ha-app-chrony-mqtt
   ```

2. Open the app from the app store.
3. Install **Chrony NTP + MQTT**, review the options and start it.

See [the documentation](chrony_ntp/DOCS.md) for the sensors, options, example automations and troubleshooting. Changes are listed in the [changelog](chrony_ntp/CHANGELOG.md).

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-app-chrony-mqtt/issues/new/choose) in this repository. Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Based on the [Chrony app](https://github.com/hassio-addons/app-chrony) of the Home Assistant Community Apps, MIT License; see [NOTICE](NOTICE).
