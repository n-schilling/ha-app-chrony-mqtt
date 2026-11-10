"""Takes the screenshots in docs/screenshots: the web page with made-up data,
served from the repository's chrony_web.py on 127.0.0.1 and taken with a
headless browser.

    uv run --with playwright==1.58.0 python scripts/screenshots.py

Uses the installed Google Chrome, else Playwright's Chromium
(`uv run --with playwright==1.58.0 playwright install chromium` once).
Every name, address and number on the pictures is made up; the addresses are
from the ranges reserved for documentation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/screenshots"
sys.path.insert(0, str(ROOT / "chrony_ntp/rootfs/usr/local/lib"))
os.environ["TZ"] = "Europe/Berlin"
time.tzset()

import chrony_web as w  # noqa: E402

NOW = 1_794_312_000.0  # 10.11.2026, 13:00
DAY = 86400
VERSION = re.search(r'^version: "?([^"\n]+)"?$', (ROOT / "chrony_ntp/config.yaml").read_text(), re.M)[1]

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
SOURCES = [("*", "192.0.2.123", 1, 10, 0o377, 380, 0.000021, 0.000094),
           ("+", "198.51.100.123", 1, 10, 0o377, 410, -0.000134, 0.000201),
           ("?", "203.0.113.123", 0, 6, 0, None, 0.0, 0.0)]
NAMES = {"192.0.2.123": "ntp1.example.net", "198.51.100.123": "ntp2.example.net",
         **{ip: c[0] for ip, c in CLIENTS.items() if c[0]}}


def read() -> dict:
    return {
        "tracking": {"refid": "C000027B", "refname": "192.0.2.123", "stratum": 2, "reftime": NOW - 380,
                     "offset": 0.000018, "rms": 0.000043, "frequency": -12.407, "skew": 0.021,
                     "root_delay": 0.004211, "root_dispersion": 0.000356, "update_interval": 1031.6,
                     "leap": "Normal"},
        "sources": [{"mode": "^", "state": st, "name": name, "stratum": stratum, "poll": poll, "reach": reach,
                     "last_rx": last, "offset": offset, "error": error}
                    for st, name, stratum, poll, reach, last, offset, error in SOURCES],
        "requests": 4211, "dropped": 2, "run": "demo",
        "clients": [{"ip": ip, "requests": c[3] % 300, "dropped": 0, "interval": c[1], "last": c[2]}
                    for ip, c in CLIENTS.items() if c[2] < 6 * 3600],
    }


def store(path: Path) -> None:
    """The store as the page keeps it after weeks of running."""
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
    totals = {"since": round(NOW - 41 * DAY), "run": "demo", "base_requests": 21890, "base_dropped": 5,
              "requests": 4211, "dropped": 2}
    path.write_text(json.dumps({"clients": clients, "totals": totals, "forgotten": {}}))


def serve(tmp: Path) -> str:
    path = tmp / "clients.json"
    store(path)
    state = w.State(w.Clients(path), w.Names(resolve=lambda ip: NAMES.get(ip, "")), read=read, clock=lambda: NOW)
    state.refresh()
    server = w.Server(("127.0.0.1", 0), w.handler(state, "a1b2c3d4_chrony_ntp", set(), VERSION))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        url = serve(Path(tmp))
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Error:
            browser = pw.chromium.launch()

        def page(width: int, height: int, scheme: str = "light", lang: str = "en-US"):
            context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                          device_scale_factor=2, locale=lang,
                                          extra_http_headers={"Accept-Language": lang})
            p = context.new_page()
            p.goto(url)
            p.wait_for_load_state("networkidle")
            return p

        p = page(1200, 760)
        p.screenshot(path=OUT / "overview.png")
        p.locator("#clients").scroll_into_view_if_needed()
        p.locator(".chip[data-show=active]").click()
        p.locator(".card", has=p.locator("#clients")).screenshot(path=OUT / "clients.png")
        p.locator(".chip[data-show=all]").click()
        p.locator("tr[data-addr='192.0.2.31'] button.edit").click()
        p.wait_for_timeout(300)
        p.screenshot(path=OUT / "edit.png")
        p.context.close()

        p = page(390, 844, "dark", "de-DE")
        p.screenshot(path=OUT / "mobile-dark.png", full_page=True)
        p.context.close()
        browser.close()
    print(f"Screenshots in {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
