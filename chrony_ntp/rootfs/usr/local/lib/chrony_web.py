"""A small read-only web page behind Home Assistant's ingress, laid out like
Home Assistant's settings: chrony's state, its upstream sources and every
client that took time from it, with the time of its last request.

chrony keeps its client log in memory only, so a restart of the app forgets
every client until it asks again; some ask once a day. The page therefore
keeps when it first and last saw each client in /data and lists clients
chrony no longer knows, until they have been away for FORGET_AFTER.

The page is rendered on the server and needs nothing beyond Python's standard
library. Its only script takes Home Assistant's theme from the page around
the ingress frame, sorts and filters the client list, and swaps in a fresh
copy of the page every REFRESH seconds.
"""

from __future__ import annotations

import html
import json
import logging
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger("chrony_web")

PORT = 8099              # the Supervisor's default ingress port
# The Supervisor's ingress proxy; nothing else may talk to the page
INGRESS_PEER = "172.30.32.2"
STORE = Path("/data/clients.json")
POLL = 15                # seconds between two reads of chronyc
REFRESH = 30             # seconds between two page updates in the browser
SAVE_EVERY = 300         # at most one write of the client store per 5 minutes
FORGET_AFTER = 30 * 86400
NAME_TTL = 3600          # reverse DNS answers, also missing ones, are kept an hour
NEVER = 4294967295       # chrony's placeholder for "never"
LOCAL_CLOCK = "7F7F0101"  # chrony's own clock (serve_when_offline)
LOCALHOST = ("127.0.0.1", "::1")

TEXT = {
    "en": {
        "title": "NTP server", "settings": "Settings", "status": "Status", "accuracy": "Accuracy", "synced": "Synchronized with {}",
        "unsynced": "Not synchronized", "local": "Serving its own clock, no upstream source",
        "leap": "Leap status: {}", "reference": "Reference source", "stratum": "Stratum",
        "offset": "System offset", "rms": "RMS offset", "frequency": "Frequency", "skew": "Skew",
        "root_delay": "Root delay", "root_dispersion": "Root dispersion", "last_sync": "Last upstream sync",
        "update_interval": "Update interval", "requests": "NTP requests", "dropped": "Dropped",
        "since_start": "since the app started", "sources": "Upstream sources", "source": "Server",
        "state": "State", "poll": "every", "reach": "Reach", "last_rx": "Last reply", "sample": "Offset",
        "no_sources": "No upstream source.", "clients": "Clients", "name": "Name", "address": "Address",
        "interval": "Interval", "last_seen": "Last request", "first_seen": "First seen",
        "all": "All", "active": "Active", "inactive": "Inactive", "search": "Search",
        "no_clients": "No client has asked for the time yet.", "no_match": "No client matches.",
        "ago": "{} ago", "unknown": "–",
        "active_info": "Active means a request within the last {}.", "chrony_down": "chronyc does not answer.", "loading": "Reading chrony …",
        "days": "d", "hours": "h", "minutes": "min", "seconds": "s",
        "states": {"*": "selected", "+": "combined", "-": "not combined", "?": "unreachable",
                   "x": "falseticker", "~": "too variable"},
    },
    "de": {
        "title": "NTP-Server", "settings": "Einstellungen", "status": "Status", "accuracy": "Genauigkeit", "synced": "Synchron mit {}",
        "unsynced": "Nicht synchronisiert", "local": "Verteilt die eigene Uhr, keine Upstream-Quelle",
        "leap": "Schaltsekunden-Status: {}", "reference": "Referenzquelle", "stratum": "Stratum",
        "offset": "Systemabweichung", "rms": "RMS-Abweichung", "frequency": "Frequenz", "skew": "Streuung",
        "root_delay": "Root Delay", "root_dispersion": "Root Dispersion", "last_sync": "Letzter Abgleich",
        "update_interval": "Abgleich-Intervall", "requests": "NTP-Anfragen", "dropped": "Verworfen",
        "since_start": "seit dem Start der App", "sources": "Upstream-Quellen", "source": "Server",
        "state": "Zustand", "poll": "alle", "reach": "Erreichbar", "last_rx": "Letzte Antwort",
        "sample": "Abweichung", "no_sources": "Keine Upstream-Quelle.", "clients": "Clients",
        "name": "Name", "address": "Adresse", "interval": "Intervall", "last_seen": "Letzte Anfrage",
        "first_seen": "Zuerst gesehen", "all": "Alle", "active": "Aktiv", "inactive": "Inaktiv",
        "search": "Suchen", "no_clients": "Noch kein Client hat nach der Zeit gefragt.",
        "no_match": "Kein Client passt.", "ago": "vor {}", "unknown": "–", "active_info": "Aktiv heißt: eine Anfrage in den letzten {}.",
        "chrony_down": "chronyc antwortet nicht.", "loading": "Chrony wird gelesen …", "days": "d", "hours": "h", "minutes": "min", "seconds": "s",
        "states": {"*": "ausgewählt", "+": "kombiniert", "-": "nicht kombiniert", "?": "nicht erreichbar",
                   "x": "falsche Zeit", "~": "zu schwankend"},
    },
}

# Material Design Icons (Apache 2.0), as Home Assistant draws them
ICONS = {
    "settings": "M12,8A4,4 0 0,1 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12A4,4 0 0,1 12,8M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10M10,22C9.75,22 9.54,21.82 9.5,21.58L9.13,18.93C8.5,18.68 7.96,18.34 7.44,17.94L4.95,18.95C4.73,19.03 4.46,18.95 4.34,18.73L2.34,15.27C2.21,15.05 2.27,14.78 2.46,14.63L4.57,12.97L4.5,12L4.57,11L2.46,9.37C2.27,9.22 2.21,8.95 2.34,8.73L4.34,5.27C4.46,5.05 4.73,4.96 4.95,5.05L7.44,6.05C7.96,5.66 8.5,5.32 9.13,5.07L9.5,2.42C9.54,2.18 9.75,2 10,2H14C14.25,2 14.46,2.18 14.5,2.42L14.87,5.07C15.5,5.32 16.04,5.66 16.56,6.05L19.05,5.05C19.27,4.96 19.54,5.05 19.66,5.27L21.66,8.73C21.79,8.95 21.73,9.22 21.54,9.37L19.43,11L19.5,12L19.43,13L21.54,14.63C21.73,14.78 21.79,15.05 21.66,15.27L19.66,18.73C19.54,18.95 19.27,19.04 19.05,18.95L16.56,17.95C16.04,18.34 15.5,18.68 14.87,18.93L14.5,21.58C14.46,21.82 14.25,22 14,22H10M11.25,4L10.88,6.61C9.68,6.86 8.62,7.5 7.85,8.39L5.44,7.35L4.69,8.65L6.8,10.2C6.4,11.37 6.4,12.64 6.8,13.8L4.68,15.36L5.43,16.66L7.86,15.62C8.63,16.5 9.68,17.14 10.87,17.38L11.24,20H12.76L13.13,17.39C14.32,17.14 15.37,16.5 16.14,15.62L18.57,16.66L19.32,15.36L17.2,13.81C17.6,12.64 17.6,11.37 17.2,10.2L19.31,8.65L18.56,7.35L16.15,8.39C15.38,7.5 14.32,6.86 13.12,6.62L12.75,4H11.25Z",
    "ok": "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z",
    "alert": "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "search": "M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z",
}


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="{ICONS[name]}"/></svg>'


# Home Assistant's defaults; the script replaces them with the theme in use
STYLE = """
:root { color-scheme: light dark; --primary-background-color: #fafafa; --secondary-background-color: #e5e5e5;
  --card-background-color: #fff; --primary-text-color: #212121; --secondary-text-color: #727272;
  --divider-color: rgba(0,0,0,.12); --primary-color: #03a9f4; --success-color: #4caf50;
  --warning-color: #ff9800; --error-color: #db4437; --disabled-text-color: #bdbdbd;
  --sidebar-background-color: #fff; --sidebar-text-color: #212121; --sidebar-icon-color: rgba(0,0,0,.5);
  --app-header-border-bottom: 1px solid var(--divider-color); --ha-card-border-radius: 12px;
  --ha-card-border-width: 1px; --ha-card-border-color: var(--divider-color); --ha-card-box-shadow: none;
  --ha-font-family-body: Roboto, Noto, sans-serif; --header-height: 56px; }
@media (prefers-color-scheme: dark) { :root { --primary-background-color: #111; --secondary-background-color: #282828;
  --card-background-color: #1c1c1c; --primary-text-color: #e1e1e1; --secondary-text-color: #9b9b9b;
  --divider-color: rgba(225,225,225,.12); --disabled-text-color: #6f6f6f; --sidebar-background-color: #1c1c1c;
  --sidebar-text-color: #e1e1e1; --sidebar-icon-color: rgba(255,255,255,.5); } }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--primary-background-color); color: var(--primary-text-color);
  font: 14px/1.6 var(--ha-font-family-body); -webkit-font-smoothing: antialiased; }
a { color: var(--primary-color); text-decoration: none; }
svg { width: 24px; height: 24px; fill: currentColor; flex: none; }
header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; height: var(--header-height);
  padding: 0 12px; background: var(--sidebar-background-color); color: var(--sidebar-text-color);
  border-bottom: var(--app-header-border-bottom); }
header h1 { margin: 0 0 0 12px; font-size: 20px; font-weight: 400; line-height: 32px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
header .actions { margin-left: auto; display: flex; }
header .actions a { display: flex; align-items: center; justify-content: center; width: 48px; height: 48px;
  border-radius: 50%; color: var(--sidebar-icon-color); }
header .actions a:hover, tbody tr:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
main { margin: 0 auto; padding: 16px; max-width: 1040px; }
.card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius);
  border: var(--ha-card-border-width) solid var(--ha-card-border-color); box-shadow: var(--ha-card-box-shadow);
  margin-bottom: 16px; overflow: hidden; }
.card h2 { margin: 0; padding: 24px 16px 16px; font-size: 24px; font-weight: 400; line-height: 32px; }
.card .content { padding: 0 16px 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 0 16px; }
.state { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-weight: 500; }
.ok { color: var(--success-color); } .bad { color: var(--warning-color); } .muted { color: var(--secondary-text-color); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 8px 16px 8px 0; vertical-align: top; }
td { border-top: 1px solid var(--divider-color); }
th { font-weight: 500; font-size: 12px; white-space: nowrap; }
table.kv th { width: 45%; font-size: 14px; font-weight: 400; color: var(--secondary-text-color);
  border-top: 1px solid var(--divider-color); white-space: normal; }
table.kv tr:first-child th, table.kv tr:first-child td { border-top: 0; }
.list th:first-child, .list td:first-child { padding-left: 16px; }
.list th { height: 56px; vertical-align: middle; border-bottom: 1px solid var(--divider-color); }
.list th[data-sort] { cursor: pointer; user-select: none; }
.list th[data-sort]:hover { color: var(--primary-color); }
.list th.asc::after { content: " ▲"; font-size: 9px; } .list th.desc::after { content: " ▼"; font-size: 9px; }
.list td { height: 52px; vertical-align: middle; font-size: 13px; }
.list tr:first-child td { border-top: 0; }
.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.nowrap { white-space: nowrap; }
.sub { display: block; font-size: 12px; line-height: 16px; color: var(--secondary-text-color); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--disabled-text-color); }
.dot.on { background: var(--success-color); }
.scroll { overflow-x: auto; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 0 16px 16px; }
.chip { display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 12px; border-radius: 8px;
  border: 1px solid var(--divider-color); color: var(--primary-text-color); background: none; font: inherit;
  font-size: 13px; font-weight: 500; cursor: pointer; }
.chip:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
.chip.on { background: color-mix(in srgb, var(--primary-color) 15%, transparent); border-color: transparent; }
.search { display: flex; align-items: center; gap: 8px; margin-left: auto; height: 36px; padding: 0 12px;
  border-radius: 18px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
.search svg { width: 20px; height: 20px; }
.search input { width: 180px; border: 0; outline: 0; background: none; color: var(--primary-text-color); font: inherit; }
.note { padding: 0 16px 16px; font-size: 12px; color: var(--secondary-text-color); }
@media (max-width: 600px) {
  .opt { display: none; }
  .search { margin-left: 0; flex: 1 0 100%; } .search input { width: 100%; }
}
"""

# Takes Home Assistant's theme from the page around the ingress frame (same
# origin) and follows it; sorts and filters the clients; refreshes the page
THEME_VARS = ("primary-background-color", "secondary-background-color", "card-background-color",
              "primary-text-color", "secondary-text-color", "divider-color", "primary-color", "success-color",
              "warning-color", "error-color", "disabled-text-color", "sidebar-background-color",
              "sidebar-text-color", "sidebar-icon-color", "app-header-border-bottom", "ha-card-border-radius",
              "ha-card-border-width", "ha-card-border-color", "ha-card-box-shadow", "ha-font-family-body",
              "header-height")
SCRIPT = """(() => {
  try {
    const outer = window.parent.document.documentElement;
    if (outer !== document.documentElement) {
      const take = () => {
        const theme = getComputedStyle(outer), own = document.documentElement.style;
        for (const n of NAMES) { const v = theme.getPropertyValue("--" + n).trim(); if (v) own.setProperty("--" + n, v); }
        const ha = window.parent.document.querySelector("home-assistant");
        if (ha && ha.hass) own.colorScheme = ha.hass.themes.darkMode ? "dark" : "light";
      };
      take();
      new MutationObserver(take).observe(outer, { attributes: true, attributeFilter: ["style", "class"] });
    }
  } catch (e) {}
  const view = { key: "last", dir: 1, show: "all", q: "" };
  const apply = () => {
    const body = document.querySelector("#clients tbody");
    if (!body) return;
    const rows = [...body.querySelectorAll("tr[data-name]")];
    const value = (r) => r.dataset[view.key];
    const number = rows.every((r) => value(r) === "" || !isNaN(value(r)));
    rows.sort((a, b) => {
      const x = value(a), y = value(b);
      if (x === "" || y === "") return (x === "") - (y === "");
      return view.dir * (number ? x - y : x.localeCompare(y, undefined, { numeric: true }));
    });
    let shown = 0;
    for (const r of rows) {
      const hit = (view.show === "all" || r.dataset.state === view.show)
        && (!view.q || r.dataset.text.includes(view.q));
      r.hidden = !hit; shown += hit; body.appendChild(r);
    }
    const none = body.querySelector("tr.none");
    if (none) none.hidden = shown > 0 || rows.length === 0;
    for (const th of document.querySelectorAll("#clients th[data-sort]")) {
      th.classList.toggle("asc", th.dataset.sort === view.key && view.dir > 0);
      th.classList.toggle("desc", th.dataset.sort === view.key && view.dir < 0);
    }
    for (const c of document.querySelectorAll(".chip[data-show]")) c.classList.toggle("on", c.dataset.show === view.show);
    const q = document.querySelector("#q");
    if (q && document.activeElement !== q) q.value = view.q;
  };
  document.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]"), chip = e.target.closest(".chip[data-show]");
    if (th) { view.dir = th.dataset.sort === view.key ? -view.dir : (th.dataset.sort === "last" ? 1 : th.classList.contains("n") ? -1 : 1);
              view.key = th.dataset.sort; apply(); }
    if (chip) { view.show = chip.dataset.show; apply(); }
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "q") { view.q = e.target.value.trim().toLowerCase(); apply(); }
  });
  document.addEventListener("DOMContentLoaded", apply);
  setInterval(async () => {
    if (document.hidden) return;
    try {
      const page = await fetch(location.href, { cache: "no-store" });
      if (!page.ok) return;
      const fresh = new DOMParser().parseFromString(await page.text(), "text/html").querySelector("main");
      const q = document.querySelector("#q"), typing = q && document.activeElement === q;
      const main = document.querySelector("main");
      if (typing) {
        fresh.querySelector("#q").replaceWith(q);
      }
      main.replaceWith(fresh);
      apply();
      if (typing) q.focus();
    } catch (e) {}
  }, REFRESH * 1000);
})();""".replace("NAMES", json.dumps(THEME_VARS)).replace("REFRESH", str(REFRESH))


# --- chronyc ----------------------------------------------------------------------
def chronyc(*args: str) -> list[list[str]]:
    """The CSV lines of `chronyc -n -c <args>`, split into fields."""
    out = subprocess.run(["chronyc", "-n", "-c", *args], capture_output=True, text=True, timeout=10, check=True)
    return [line.split(",") for line in out.stdout.splitlines() if line.strip()]


def number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def read_chrony() -> dict:
    """chrony's tracking, sources, server statistics and clients as plain values."""
    t = chronyc("tracking")[0]
    tracking = {
        "refid": t[0], "refname": t[1], "stratum": int(t[2]), "reftime": number(t[3]),
        "offset": number(t[4]), "rms": number(t[6]), "frequency": number(t[7]), "skew": number(t[9]),
        "root_delay": number(t[10]), "root_dispersion": number(t[11]), "update_interval": number(t[12]),
        "leap": t[13],
    }
    sources = [{"mode": s[0], "state": s[1], "name": s[2], "stratum": int(s[3]), "poll": int(s[4]),
                "reach": int(s[5], 8), "last_rx": None if int(s[6]) >= NEVER else int(s[6]), "offset": number(s[7]), "error": number(s[9])}
               for s in chronyc("sources")]
    stats = chronyc("serverstats")[0]
    clients = []
    for c in chronyc("clients"):
        if c[0] in LOCALHOST:
            continue
        clients.append({"ip": c[0], "requests": int(c[1]), "dropped": int(c[2]),
                        "interval": None if c[3] in ("-", "127") else int(c[3]),
                        "last": None if int(c[5]) >= NEVER else int(c[5])})
    return {"tracking": tracking, "sources": sources, "requests": int(stats[0]), "dropped": int(stats[1]),
            "clients": clients}


def sync_problem(tracking: dict) -> bool:
    """The same rule as the Sync problem sensor."""
    return tracking["stratum"] >= 16 or tracking["leap"] != "Normal" or tracking["refid"] == LOCAL_CLOCK


# --- Names and the client store ------------------------------------------------------
class Names:
    """Host names of client addresses by reverse DNS, as the router hands them out."""

    def __init__(self, resolve=None) -> None:
        self._resolve = resolve or self._lookup
        self._known: dict[str, tuple[float, str]] = {}

    @staticmethod
    def _lookup(ip: str) -> str:
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            return ""
        # Without an entry musl answers with the address itself
        return "" if name == ip else name

    def get(self, ip: str, now: float) -> str:
        known = self._known.get(ip)
        if known is None or now - known[0] > NAME_TTL:
            known = (now, self._resolve(ip))
            self._known[ip] = known
        return known[1]


class Clients:
    """When each client was first and last seen, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self._saved = 0.0
        try:
            self.data = json.loads(path.read_text())["clients"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def update(self, clients: list[dict], names: dict[str, str], now: float) -> None:
        # A client without an NTP request only used chronyc
        for c in (c for c in clients if c["last"] is not None):
            seen = round(now - c["last"])
            entry = self.data.setdefault(c["ip"], {"first_seen": seen, "last_seen": seen, "name": ""})
            entry["last_seen"] = max(entry["last_seen"], seen)
            entry["first_seen"] = min(entry["first_seen"], seen)
            entry["name"] = names.get(c["ip"]) or entry["name"]
        for ip in [ip for ip, e in self.data.items() if now - e["last_seen"] > FORGET_AFTER]:
            del self.data[ip]
        if now - self._saved >= SAVE_EVERY:
            self.save(now)

    def save(self, now: float) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"clients": self.data}, sort_keys=True))
            os.replace(tmp, self.path)
            self._saved = now
        except OSError as err:
            LOGGER.warning("Cannot save %s: %s", self.path, err)


class State:
    """The latest read of chrony, renewed in the background every POLL seconds."""

    def __init__(self, clients: Clients, names: Names | None = None, read=read_chrony, clock=time.time) -> None:
        self.clients = clients
        self.names = names or Names()
        self._read = read
        self._clock = clock
        self.lock = threading.Lock()
        self.chrony: dict | None = None
        self.at = 0.0

    def refresh(self) -> None:
        now = self._clock()
        try:
            chrony = self._read()
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as err:
            LOGGER.debug("chronyc failed: %s", err)
            chrony = None
        # Lookups can take seconds; the page keeps showing the last read meanwhile
        names = {c["ip"]: self.names.get(c["ip"], now) for c in (chrony or {}).get("clients", [])
                 if c["last"] is not None}
        if chrony is not None:
            # chronyc's CSV output has addresses only, also for the upstream servers
            for source in chrony["sources"]:
                source["host"] = self.names.get(source["name"], now)
            refname = chrony["tracking"]["refname"]
            chrony["tracking"]["host"] = self.names.get(refname, now) if refname else ""
        with self.lock:
            self.chrony, self.at = chrony, now
            if chrony is not None:
                self.clients.update(chrony["clients"], names, now)

    def run(self) -> None:
        while True:
            self.refresh()
            time.sleep(POLL)


# --- Formatting --------------------------------------------------------------------------
def lang_of(header: str | None) -> str:
    """German if the browser puts German before English, else English."""
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()
        if code.startswith("de"):
            return "de"
        if code.startswith("en"):
            return "en"
    return "en"


class Format:
    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.t = TEXT[lang]

    def dec(self, value: float, digits: int) -> str:
        text = f"{value:.{digits}f}".replace("-", "−")
        return text.replace(".", ",") if self.lang == "de" else text

    def int(self, value: int) -> str:
        text = f"{value:,}"
        return text.replace(",", ".") if self.lang == "de" else text

    def ms(self, seconds: float | None) -> str:
        return self.t["unknown"] if seconds is None else f"{self.dec(seconds * 1000, 3)} ms"

    def duration(self, seconds: float) -> str:
        """Two units at most: 45 s, 12 min, 3 h 5 min, 2 d 4 h."""
        t = self.t
        s = round(seconds)
        if seconds < 1:
            return f"{self.dec(seconds, 1)} {t['seconds']}"
        if s < 60:
            return f"{s} {t['seconds']}"
        if s < 3600:
            return f"{s // 60} {t['minutes']}"
        if s < 86400:
            h, m = divmod(s // 60, 60)
            return f"{h} {t['hours']} {m} {t['minutes']}" if m else f"{h} {t['hours']}"
        d, h = divmod(s // 3600, 24)
        return f"{d} {t['days']} {h} {t['hours']}" if h else f"{d} {t['days']}"

    def ago(self, seconds: float) -> str:
        return self.t["ago"].format(self.duration(max(seconds, 0)))

    def when(self, epoch: float | None) -> str:
        if epoch is None:
            return self.t["unknown"]
        return time.strftime("%d.%m.%Y %H:%M:%S" if self.lang == "de" else "%Y-%m-%d %H:%M:%S",
                             time.localtime(epoch))

    def log2(self, exponent: int | None) -> str:
        """chrony's intervals are powers of two."""
        return self.t["unknown"] if exponent is None else self.duration(2.0 ** exponent)


# --- The page ------------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value), quote=True)


def kv(rows: list[tuple[str, str]]) -> str:
    return '<table class="kv">' + "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def card(title: str | None, content: str, padded: bool = True, attrs: str = "") -> str:
    head = f"<h2>{esc(title)}</h2>" if title else ""
    inner = f'<div class="content">{content}</div>' if padded else content
    return f'<div class="card"{attrs}>{head}{inner}</div>'


def status_card(f: Format, chrony: dict, now: float) -> str:
    t, tr = f.t, chrony["tracking"]
    reference = tr.get("host") or tr["refname"] or tr["refid"]
    if not sync_problem(tr):
        state = f'<div class="state ok">{icon("ok")}{esc(t["synced"].format(reference))}</div>'
    else:
        reason = (t["local"] if tr["refid"] == LOCAL_CLOCK else
                  t["unsynced"] if tr["stratum"] >= 16 else t["leap"].format(tr["leap"]))
        state = f'<div class="state bad">{icon("alert")}{esc(reason)}</div>'
    last_sync = (f'{esc(f.ago(now - tr["reftime"]))}<span class="sub">{esc(f.when(tr["reftime"]))}</span>'
                 if tr["reftime"] else t["unknown"])
    return card(t["status"], state + kv([
        (t["reference"], esc(reference)),
        (t["stratum"], esc(tr["stratum"])),
        (t["last_sync"], last_sync),
        (t["offset"], esc(f.ms(tr["offset"]))),
        (t["requests"], f'{esc(f.int(chrony["requests"]))}<span class="sub">{esc(t["since_start"])}</span>'),
        (t["dropped"], esc(f.int(chrony["dropped"]))),
    ]))


def accuracy_card(f: Format, chrony: dict) -> str:
    t, tr = f.t, chrony["tracking"]
    return card(t["accuracy"], kv([
        (t["rms"], esc(f.ms(tr["rms"]))),
        (t["frequency"], esc(f"{f.dec(tr['frequency'] or 0, 3)} ppm")),
        (t["skew"], esc(f"{f.dec(tr['skew'] or 0, 3)} ppm")),
        (t["root_delay"], esc(f.ms(tr["root_delay"]))),
        (t["root_dispersion"], esc(f.ms(tr["root_dispersion"]))),
        (t["update_interval"], esc(f.duration(tr["update_interval"] or 0))),
    ]))


def sources_card(f: Format, chrony: dict) -> str:
    t = f.t
    rows = []
    for s in chrony["sources"]:
        state = t["states"].get(s["state"], s["state"])
        cls = "ok" if s["state"] == "*" else "bad" if s["state"] in "?x~" else ""
        reach = bin(s["reach"]).count("1")
        host = s.get("host") or s["name"]
        rows.append(f'<tr><td title="{esc(s["name"])}">{esc(host)}<span class="sub">{esc(t["stratum"])} {esc(s["stratum"])} · '
                    f'{esc(t["poll"])} {esc(f.log2(s["poll"]))}</span></td>'
                    f'<td class="{cls}">{esc(state)}</td>'
                    f'<td class="n" title="{s["reach"]:03o}">{reach}/8</td>'
                    f'<td class="n">{esc(t["unknown"] if s["last_rx"] is None else f.ago(s["last_rx"]))}</td>'
                    f'<td class="n opt" title="± {esc(f.ms(s["error"]))}">{esc(f.ms(s["offset"]))}</td></tr>')
    body = "".join(rows) or f'<tr><td colspan="5" class="muted">{esc(t["no_sources"])}</td></tr>'
    return card(t["sources"], (
        f'<div class="scroll"><table class="list"><thead><tr><th>{esc(t["source"])}</th><th>{esc(t["state"])}</th>'
        f'<th class="n">{esc(t["reach"])}</th><th class="n">{esc(t["last_rx"])}</th>'
        f'<th class="n opt">{esc(t["sample"])}</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"), padded=False)


def short_name(name: str) -> str:
    """The host part of a name the router hands out: device-name.lan → device-name."""
    return name.split(".", 1)[0] if name and not name.replace(".", "").isdigit() else name


def clients_card(f: Format, state: State, window: int, now: float) -> str:
    t = f.t
    live = {c["ip"]: c for c in (state.chrony or {}).get("clients", [])}
    rows = []
    active = 0
    for ip, entry in state.clients.data.items():
        c = live.get(ip)
        last_seen = entry["last_seen"]
        ago = now - last_seen
        is_active = ago <= window
        active += is_active
        name = entry["name"]
        shown = short_name(name) or ip
        text = " ".join((name, ip)).lower()
        last = f'{esc(f.ago(ago))}<span class="sub">{esc(f.when(last_seen))}</span>'
        requests = esc(f.int(c["requests"])) if c else t["unknown"]
        dropped = esc(f.int(c["dropped"])) if c else t["unknown"]
        interval = esc(f.log2(c["interval"])) if c else t["unknown"]
        sort_ip = ".".join(p.zfill(3) for p in ip.split(".")) if "." in ip else ip
        rows.append(
            f'<tr data-name="{esc(shown.lower())}" data-ip="{esc(sort_ip)}" data-text="{esc(text)}"'
            f' data-state="{"active" if is_active else "inactive"}"'
            f' data-requests="{c["requests"] if c else ""}" data-dropped="{c["dropped"] if c else ""}"'
            f' data-interval="{"" if not c or c["interval"] is None else c["interval"]}"'
            f' data-last="{round(ago)}" data-first="{entry["first_seen"]}">'
            f'<td><span class="dot{" on" if is_active else ""}" title="{esc(t["active" if is_active else "inactive"])}"></span></td>'
            f'<td title="{esc(name)}">{esc(shown)}{f'<span class="sub">{esc(ip)}</span>' if shown != ip else ""}</td>'
            f'<td class="n">{requests}</td><td class="n opt">{dropped}</td><td class="n opt">{interval}</td>'
            f'<td class="n">{last}</td><td class="n opt">{esc(f.when(entry["first_seen"]))}</td></tr>')
    total = len(rows)
    empty = "" if rows else f'<tr><td colspan="7" class="muted">{esc(t["no_clients"])}</td></tr>'
    rows.append(f'<tr class="none" hidden><td colspan="7" class="muted">{esc(t["no_match"])}</td></tr>')
    chips = "".join(f'<button class="chip{" on" if key == "all" else ""}" data-show="{key}">{esc(t[key])} · {n}</button>'
                    for key, n in (("all", total), ("active", active), ("inactive", total - active)))
    toolbar = (f'<div class="toolbar">{chips}<label class="search">{icon("search")}'
               f'<input id="q" type="search" placeholder="{esc(t["search"])}" autocomplete="off"></label></div>')
    head = (f'<thead><tr><th></th><th data-sort="name">{esc(t["name"])}</th>'
            f'<th class="n" data-sort="requests">{esc(t["requests"])}</th>'
            f'<th class="n opt" data-sort="dropped">{esc(t["dropped"])}</th>'
            f'<th class="n opt" data-sort="interval">{esc(t["interval"])}</th>'
            f'<th class="n" data-sort="last">{esc(t["last_seen"])}</th>'
            f'<th class="n opt" data-sort="first">{esc(t["first_seen"])}</th></tr></thead>')
    note = f'<div class="note">{esc(t["active_info"].format(f.duration(window)))}</div>'
    return card(t["clients"], toolbar + f'<div class="scroll"><table class="list" id="clients">{head}'
                f'<tbody>{"".join(rows)}{empty}</tbody></table></div>' + note, padded=False)


def render(state: State, lang: str, window: int, slug: str | None, now: float | None = None) -> str:
    f = Format(lang)
    t = f.t
    now = state._clock() if now is None else now
    with state.lock:
        chrony = state.chrony
        if chrony is None:
            body = card(t["status"], f'<p class="muted">{esc(t["loading"])}</p>' if not state.at else
                        f'<div class="state bad">{icon("alert")}{esc(t["chrony_down"])}</div>')
        else:
            body = f'<div class="cards">{status_card(f, chrony, now)}<div>{sources_card(f, chrony)}{accuracy_card(f, chrony)}</div></div>'
        body += clients_card(f, state, window, now)
    # The settings are Home Assistant's own page, outside the ingress frame
    actions = (f'<a href="/config/app/{esc(slug)}/config" target="_top" title="{esc(t["settings"])}">'
               f'{icon("settings")}</a>' if slug else "")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(t['title'])}</title><style>{STYLE}</style><script>{SCRIPT}</script></head><body>"
            f'<header><h1>{esc(t["title"])}</h1><div class="actions">{actions}</div></header>'
            f"<main>{body}</main></body></html>")


# --- The server ------------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer looks up its own name here, which can wait for DNS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "chrony-web", self.server_address[1]


def handler(state: State, window: int, slug: str | None, allowed: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "chrony-web"

        def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
            if allowed and self.client_address[0] not in allowed:
                self._send(403, "text/plain", b"only through Home Assistant")
            elif self.path.split("?", 1)[0] != "/":
                self._send(404, "text/plain", b"not found")
            else:
                page = render(state, lang_of(self.headers.get("Accept-Language")), window, slug)
                self._send(200, "text/html; charset=utf-8", page.encode())

        def _send(self, code: int, kind: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # no access log
            pass

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.DEBUG if os.environ.get("LOG_LEVEL") == "debug" else logging.INFO,
                        format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    window = int(os.environ.get("ACTIVE_WINDOW", "3900"))
    # The app's slug, e.g. 8af13b69_chrony_ntp; the Supervisor names the container's host after it
    slug = os.environ.get("HOSTNAME", "").replace("-", "_") or None
    state = State(Clients(STORE))
    threading.Thread(target=state.run, daemon=True).start()
    # s6 stops the service with TERM; leave through finally to save the store
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), handler(state, window, slug, {INGRESS_PEER}))
    LOGGER.info("Web page on port %d (ingress)", PORT)
    try:
        server.serve_forever()
    finally:
        state.clients.save(time.time())


if __name__ == "__main__":
    main()
