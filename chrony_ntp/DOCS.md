# Chrony NTP + MQTT

Runs chrony as an NTP server on port 123/udp, so devices in your network can take their time from Home Assistant instead of the internet, and publishes chrony's key figures as Home Assistant sensors through MQTT discovery.

## Requirements

- An MQTT broker known to the Supervisor, e.g. the Mosquitto broker app with the MQTT integration
- Port 123/udp free on the host

## Options

| Option | Default | Meaning |
|---|---|---|
| `mode` | `pool` | `pool`: one pool name that resolves to several servers; `server`: a list of servers |
| `ntp_pool` | `pool.ntp.org` | Pool used in pool mode |
| `ntp_server` | `0–2.pool.ntp.org` | Servers used in server mode |
| `set_system_clock` | `true` | Let chrony adjust the host clock; off makes chrony only serve time |
| `allowed_networks` | | Networks that may take time from this server, e.g. `192.168.1.0/24` or `fd00::/8`; empty serves every client that reaches port 123 |
| `serve_when_offline` | `false` | Keep serving the host clock at stratum 10 when no upstream server is reachable, so clients stay in step with each other; the *Sync problem* sensor turns on meanwhile |
| `mqtt_interval` | `300` | Seconds between two sensor updates |
| `active_window` | `3900` | Seconds since its last request within which a client counts as active |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
| `log_level` | `info` | How much the app writes to its log: `info`, `warning` or `error` |

## Sensors

Device **Chrony NTP Server**:

| Sensor | Meaning |
|---|---|
| Clients | Clients chrony has seen; attributes list each with requests, drops and seconds since its last request |
| Active clients | Clients within the active window |
| NTP requests | NTP packets received |
| NTP requests dropped | NTP packets dropped |
| Stratum | chrony's stratum |
| Last upstream sync | Time of the last update from the upstream source |
| System offset | Offset of the system clock, in ms |
| RMS offset | Long-term average offset, in ms |
| Root dispersion | Accumulated dispersion to the root source, in ms |
| Reference source | Upstream source chrony follows |
| Sync problem | On while unsynchronized (stratum 16), serving its own clock (`serve_when_offline`) or a leap status other than normal |

All entities turn unavailable when the app stops or the connection to the broker breaks.

## Pointing devices to it

Give your devices the Home Assistant host as NTP server, or redirect NTP from a network segment to it on your router.

To check it from a computer in your network:

```bash
sntp <home-assistant-host>
```

## Example automations

Notify when the server loses its upstream time source for 15 minutes:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.chrony_ntp_server_sync_problem
    to: "on"
    for: "00:15:00"
actions:
  - action: notify.notify
    data:
      message: "The NTP server has no upstream time source ({{ states('sensor.chrony_ntp_server_reference_source') }})."
```

Notify when no device has asked for the time for an hour, e.g. after a router change broke the NTP redirect:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.chrony_ntp_server_active_clients
    below: 1
    for: "01:00:00"
actions:
  - action: notify.notify
    data:
      message: "No device has taken its time from the NTP server for an hour."
```

The entity IDs follow the device name; adjust them if you renamed the device.

## Troubleshooting

- **The app does not start, port 123 is in use.** Another app or service on the host serves NTP. Stop it, or map the app's port to another one on the *Configuration* tab and point your devices there.
- **Clients stay at 0.** Devices only show up after they asked for the time. Check that they use the Home Assistant host as NTP server, that a redirect on the router points to it, and that `allowed_networks` includes their network.
- **Sync problem stays on.** The host cannot reach the upstream servers. Check outgoing UDP port 123 on your router and the servers in `ntp_pool` / `ntp_server`; the log shows chrony's view.
- **The entities are unavailable.** The app is stopped, or it lost its connection to the MQTT broker; the log tells which.

## Known limitations

- One NTP server per Home Assistant host: port 123 can only be served once.
- `set_system_clock` changes the clock of the whole host. Turn it off if something else, e.g. Home Assistant OS's own time sync, should stay in charge.
- Client statistics come from chrony's client log, which only covers recent clients and resets when the app restarts.

## Removing the app

The sensors are announced with retained MQTT messages and stay in Home Assistant after the app is removed. Delete the device **Chrony NTP Server** under **Settings → Devices & services → MQTT**; Home Assistant then removes the retained messages as well.

## Support

Questions, bugs and ideas: open an issue at https://github.com/n-schilling/ha-app-chrony-mqtt/issues. Please add the app version and the app log.
