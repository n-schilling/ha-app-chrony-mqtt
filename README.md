# Chrony NTP + MQTT app for Home Assistant

[![Release](https://img.shields.io/github/v/release/n-schilling/ha-app-chrony-mqtt)](https://github.com/n-schilling/ha-app-chrony-mqtt/releases)
[![CI](https://github.com/n-schilling/ha-app-chrony-mqtt/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/n-schilling/ha-app-chrony-mqtt/actions/workflows/ci.yaml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5?logo=homeassistant&logoColor=white)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

![Chrony NTP + MQTT](chrony_ntp/logo.png)

A Home Assistant app that runs [chrony](https://chrony-project.org/) as a local NTP server for your network with a page of its own in Home Assistant that shows the sync status, the upstream sources and every client with the time of its last request, a name and a note. Two problem sensors through MQTT discovery tell Home Assistant when the server loses its upstream source or no client asks any more.

## The web page

The page (**Open web UI** on the app's page, or in the sidebar with **Show in sidebar**) looks like Home Assistant's settings and takes over the theme in use. The screenshots show made-up demo data.

| Status and upstream sources | Clients |
|---|---|
| ![Sync status, request totals, upstream sources and accuracy](docs/screenshots/overview.png) | ![Clients with requests, interval and last request, filtered by state](docs/screenshots/clients.png) |
| **Name and note of a client** | |
| ![Dialog to name a client, add a note or forget it](docs/screenshots/edit.png) | |

## Installation

1. Add the repository to your Home Assistant instance:

   [![Add the repository to My Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fn-schilling%2Fha-app-chrony-mqtt)

   Or add it manually: **Settings → Apps → Install app → ⋮ → Repositories**, then paste:

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
