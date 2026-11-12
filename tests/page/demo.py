"""The app's web page with made-up data, inside the built image, for the Page
check and the screenshots (scripts/screenshots.py):

    docker run --hostname a1b2c3d4-time-server -v "$PWD/tests:/tests:ro" \\
        --entrypoint python3 <image> /tests/page/demo.py

Writes a client store as the page keeps it after weeks of running and a
chronyc that answers with made-up CSV, then starts the page exactly as the
app does (chrony_web.main: port 8099, only the ingress proxy 172.30.32.2 may
connect, slug from the host name, version from APP_VERSION). Reverse DNS
answers come from the table below. Every name, address and number is made
up; the addresses are from the ranges reserved for documentation. Times are
relative to the start, so the page looks the same whenever it runs.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/lib")

import chrony_web as w  # noqa: E402

NOW = time.time()
DAY = 86400

# address: (reverse DNS name, interval log2, seconds since the last request,
#           requests, dropped, days since first seen, name given, note)
CLIENTS = {
    "192.0.2.11": ("plug-kitchen.lan", 11, 840, 1630, 0, 41, "", ""),
    "192.0.2.12": ("plug-washer.lan", 11, 1500, 1622, 0, 41, "Washing machine plug", ""),
    "192.0.2.14": ("thermostat-hall.lan", 10, 400, 3310, 2, 41, "", ""),
    "192.0.2.21": ("speaker-living.lan", 12, 2900, 812, 0, 38, "", ""),
    "192.0.2.22": ("speaker-bath.lan", 12, 3300, 798, 0, 38, "", ""),
    "192.0.2.31": ("purifier-bedroom.lan", 9, 200, 6620, 0, 30, "Air purifier bedroom",
                   "Firmware asks pool.ntp.org, redirected on the router"),
    "192.0.2.40": ("", 16, 52000, 39, 0, 41, "Wall display", "Asks about once a day"),
    "192.0.2.52": ("camera-garden.lan", 10, 700, 3405, 0, 27, "", ""),
    "192.0.2.53": ("camera-door.lan", 10, 650, 3398, 0, 27, "", ""),
    "198.51.100.7": ("laptop.lan", 10, 9 * 3600, 260, 0, 12, "", ""),
    "2001:db8::17": ("", 8, 30000, 512, 0, 3, "", ""),
}
# Clients of the HTTP time service: address: (reverse DNS name, seconds since
# the last request, requests, interval log2); one also asks over NTP
HTTP_CLIENTS = {
    "192.0.2.61": ("controller-garage.lan", 1800, 214, 12),
    "192.0.2.62": ("", 300, 1310, 11),
    "192.0.2.14": ("thermostat-hall.lan", 3500, 12, 12),
}
# state, address, stratum, poll, reach (octal), seconds since the last answer, offset, error
SOURCES = [("*", "192.0.2.123", 1, 10, "377", 380, 0.000021, 0.000094),
           ("+", "198.51.100.123", 1, 10, "377", 410, -0.000134, 0.000201),
           ("?", "203.0.113.123", 0, 6, "0", w.NEVER, 0.0, 0.0)]
NAMES = {"192.0.2.123": "ntp1.example.net", "198.51.100.123": "ntp2.example.net",
         **{ip: c[0] for ip, c in CLIENTS.items() if c[0]},
         **{ip: c[0] for ip, c in HTTP_CLIENTS.items() if c[0]}}


def chronyc(folder: Path) -> None:
    """A chronyc on PATH that answers `chronyc -n -c <what>` from made-up CSV."""
    (folder / "tracking.csv").write_text(
        f"C000027B,192.0.2.123,2,{NOW - 380:.9f},0.000018000,0.000021000,0.000043000,"
        "-12.407,0.000,0.021,0.004211000,0.000356000,1031.6,Normal\n")
    (folder / "sources.csv").write_text("".join(
        f"^,{state},{ip},{stratum},{poll},{reach},{last},{offset:.9f},{offset:.9f},{error:.9f}\n"
        for state, ip, stratum, poll, reach, last, offset, error in SOURCES))
    (folder / "serverstats.csv").write_text("4211,2,0,0,0,0,0,0,0,0,0,0,4211,4211,0,0,0\n")
    (folder / "clients.csv").write_text("127.0.0.1,4,0,-,-,10,0,0,0,-\n" + "".join(
        f"{ip},{c[3] % 300},0,{c[1]},-,{c[2]},0,0,0,-\n" for ip, c in CLIENTS.items() if c[2] < 6 * 3600))
    stub = folder / "chronyc"
    stub.write_text(f'#!/bin/sh\nfor a; do what=$a; done\nexec cat "{folder}/$what.csv"\n')
    stub.chmod(0o755)
    os.environ["PATH"] = f"{folder}:{os.environ['PATH']}"


def store(path: Path) -> None:
    """The client store as the page keeps it after weeks of running."""
    clients = {}
    for ip, (name, interval, last, requests, dropped, days, alias, note) in CLIENTS.items():
        entry = {"first_seen": round(NOW - days * DAY), "last_seen": round(NOW - last), "name": name,
                 "interval": interval, "base_requests": requests - requests % 300, "base_dropped": dropped,
                 "requests": requests % 300, "dropped": 0}
        if alias:
            entry["alias"] = alias
        if note:
            entry["note"] = note
        clients[ip] = entry
    totals = {"since": round(NOW - 41 * DAY), "base_requests": 21890, "base_dropped": 5,
              "requests": 4211, "dropped": 2}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"clients": clients, "totals": totals, "forgotten": {}}))


def http_log(path: Path) -> None:
    """The HTTP time service's client log, as it writes it."""
    clients = {ip: {"requests": requests, "dropped": 0, "last": NOW - last, "interval": interval}
               for ip, (_, last, requests, interval) in HTTP_CLIENTS.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run": "demo", "requests": sum(c["requests"] for c in clients.values()),
                                "dropped": 0, "clients": clients}))


def resolve(ip: str, *_):
    if ip in NAMES:
        return NAMES[ip], [], [ip]
    raise socket.herror(1, "Unknown host")


def main() -> None:
    folder = Path(tempfile.mkdtemp(prefix="chrony-demo-"))
    chronyc(folder)
    store(w.STORE)
    http_log(w.HTTP_LOG)
    w.STATUS.parent.mkdir(parents=True, exist_ok=True)
    socket.gethostbyaddr = resolve
    w.main()


if __name__ == "__main__":
    main()
