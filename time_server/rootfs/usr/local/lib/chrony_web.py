"""A small web page behind Home Assistant's ingress, laid out like Home
Assistant's settings: the current time, chrony's state, its upstream sources
and every client that took time from it, over NTP or HTTP, with the time of
its last request.

A client blocked on the page gets no time any more: chronyd denies its address
(chronyc deny, given again to every new chronyd) and the HTTP time service
closes its connections (BLOCKED). A blocked client stays in the list until it
is unblocked.

The HTTP time service (time_http.py) runs apart, as a user of its own, and
writes what each client asked to HTTP_LOG; the page takes it in like chrony's
client log, with counters of their own per client.

chrony keeps its counters and client log in memory only, so a restart of the
app starts its request counters at zero and forgets every client until it
asks again; some ask once a day. The page therefore keeps the request totals,
of the server and of each client, when it first and last saw each client and
its request interval in /data/web, and lists clients chrony no longer knows, until
they have been away for FORGET_AFTER. A client counts as active while its last
request is less than three of its intervals and GRACE ago. A name and a note
given to a client on the page are kept with it, and such a client stays until
it is forgotten on the page.

Every read of chrony also writes STATUS, from which the key figure publisher
takes the Requests missing sensor: no client of the last RECENT asked
within its usual time, so the server or the way to it is broken.

The page is rendered on the server and needs nothing beyond Python's standard
library. Its only script takes Home Assistant's theme from the page around
the ingress frame, sorts and filters the client list, edits a client's name
and note, and swaps in a fresh copy of the page every REFRESH seconds or when
the refresh button is pressed.
"""

from __future__ import annotations

import contextlib
import functools
import html
import ipaddress
import json
import logging
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

LOGGER = logging.getLogger("chrony_web")

PORT = 8099              # the Supervisor's default ingress port
# The Supervisor's ingress proxy; nothing else may talk to the page
INGRESS_PEER = "172.30.32.2"
APP_NAME = "Time Server"
STORE = Path("/data/web/clients.json")
STATUS = Path("/run/chrony-web/status.json")  # read by the key figure publisher
HTTP_LOG = Path("/run/time-http/clients.json")  # written by the HTTP time service
# The blocked addresses, read by the HTTP time service; chronyd learns them by chronyc
BLOCKED = Path("/run/chrony-web/blocked.json")
ALLOWED = Path("/run/time-http/allowed")  # the networks chrony serves, written by init-chrony
BLOCK_MAX = 256          # addresses blocked at most
PIDFILE = Path("/run/chrony/chronyd.pid")
COUNTERS = ("requests", "dropped")
POLL = 15                # seconds between two reads of chronyc
REFRESH = 30             # seconds between two page updates in the browser
MEASURE_EVERY = 60       # seconds between two measurements of the browser's clock
SAMPLES = 8              # round trips per measurement; the shortest counts
SAVE_EVERY = 300         # at most one write of the client store per 5 minutes
FORGET_AFTER = 30 * 86400
# Only clients that asked within this time count for Requests missing, so a
# device taken away, also one with a name, stops holding it on after a week
RECENT = 7 * 86400
# Clients kept at most: whoever reaches port 123 can make chrony see new
# addresses, so the store, the page and the name lookups stay bounded
MAX_CLIENTS = 1000
NAME_TTL = 3600          # reverse DNS answers, also missing ones, are kept an hour
NAMES_MAX = 2 * MAX_CLIENTS + 64  # answers kept at most
LOOKUPS_MAX = 256        # addresses waiting for a reverse DNS answer at most
WARN_EVERY = 3600        # one warning per hour about a full client list
# Clients poll irregularly: active while the last request is less than three
# intervals and an hour ago
GRACE = 3600
DEFAULT_INTERVAL = 10    # log2 seconds; chrony knows no interval before a second request
NAME_MAX = 40            # characters of a name given on the page
NOTE_MAX = 120           # characters of a note
BODY_MAX = 4096          # bytes of a change sent by the page
# Characters that do not belong in a line of plain text: controls, and the
# bidirectional overrides that could make a name read differently
BIDI = set("‪‫‬‭‮⁦⁧⁨⁩")
NEVER = 4294967295       # chrony's placeholder for "never"
LOCAL_CLOCK = "7F7F0101"  # chrony's own clock (serve_when_offline)
LOCALHOST = ("127.0.0.1", "::1")
# The look all the apps' pages share: /usr/local/share/ha-app/ha.css in the
# image, the same place in the repository for the tests
STYLESHEET = Path(__file__).resolve().parents[1] / "share/ha-app/ha.css"
# Home Assistant's language for the page (cookie ha_lang), shared like the
# stylesheet; it runs first, in the page's one script
SHARED_SCRIPT_FILE = STYLESHEET.with_name("ha-lang.js")

TEXT = {
    "en": {
        "title": APP_NAME, "settings": "Settings", "refresh": "Refresh", "as_of": "As of {}", "status": "Status", "accuracy": "Accuracy", "synced": "Synchronized with {}",
        "unsynced": "Not synchronized", "local": "Serving its own clock, no upstream source",
        "leap": "Leap status: {}", "reference": "Reference source", "stratum": "Stratum",
        "offset": "System offset", "rms": "RMS offset", "frequency": "Frequency", "skew": "Skew",
        "root_delay": "Root delay", "root_dispersion": "Root dispersion", "last_sync": "Last upstream sync",
        "update_interval": "Update interval", "requests": "NTP requests", "dropped": "Dropped",
        "since_start": "{} since app start", "since": "since {}", "sources": "Upstream sources", "source": "Server",
        "state": "State", "poll": "every", "reach": "Reach", "last_rx": "Last reply", "sample": "Offset",
        "no_sources": "No upstream source.", "clients": "Clients", "name": "Name", "address": "Address",
        "interval": "Interval", "last_seen": "Last request", "first_seen": "First seen",
        "all": "All", "active": "Active", "inactive": "Inactive", "search": "Search",
        "no_clients": "No client has asked for the time yet.", "no_match": "No client matches.",
        "ago": "{} ago", "unknown": "–",
        "active_info": "Active means the last request is less than three of the client's intervals and {} ago.",
        "counted": "Requests and drops count since first seen, at the earliest since {}.",
        "missing": "No client asks any more, last request {}", "expected": "expected every {}",
        "edit": "Edit", "edit_title": "Edit client", "alias": "Name", "note": "Note", "save": "Save",
        "cancel": "Cancel", "forget": "Forget",
        "forget_confirm": "Forget this client? It is listed again with its next request, counted from then on.",
        "save_failed": "Could not save.",
        "chrony_down": "chronyc does not answer.", "loading": "Reading chrony …",
        "days": "d", "hours": "h", "minutes": "min", "seconds": "s",
        "states": {"*": "selected", "+": "combined", "-": "not combined", "?": "unreachable",
                   "x": "falseticker", "~": "too variable"},
        "overview": "Overview", "upstream": "Upstream", "clients_summary": "{} active, {} inactive",
        "http_requests": "HTTP requests", "all_requests": "Requests", "protocol": "Protocol",
        "now": "Current time", "device_clock": "This device's clock", "ahead": "{} ahead", "behind": "{} behind",
        "in_step": "in step", "measured": "measured through Home Assistant to ± {}", "measuring": "Measuring …",
        "no_time": "chrony hands out no time right now.",
        "block": "Block", "unblock": "Unblock", "blocked": "Blocked",
        "block_confirm": "Block this client? It gets no time any more, over NTP and HTTP, until you unblock it.",
        "blocked_note": "Blocked: gets no time",
    },
    "de": {
        "title": APP_NAME, "settings": "Einstellungen", "refresh": "Aktualisieren", "as_of": "Stand {}", "status": "Status", "accuracy": "Genauigkeit", "synced": "Synchron mit {}",
        "unsynced": "Nicht synchronisiert", "local": "Verteilt die eigene Uhr, keine Upstream-Quelle",
        "leap": "Schaltsekunden-Status: {}", "reference": "Referenzquelle", "stratum": "Stratum",
        "offset": "Systemabweichung", "rms": "RMS-Abweichung", "frequency": "Frequenz", "skew": "Streuung",
        "root_delay": "Root Delay", "root_dispersion": "Root Dispersion", "last_sync": "Letzter Abgleich",
        "update_interval": "Abgleich-Intervall", "requests": "NTP-Anfragen", "dropped": "Verworfen",
        "since_start": "{} seit App-Start", "since": "seit {}", "sources": "Upstream-Quellen", "source": "Server",
        "state": "Zustand", "poll": "alle", "reach": "Erreichbar", "last_rx": "Letzte Antwort",
        "sample": "Abweichung", "no_sources": "Keine Upstream-Quelle.", "clients": "Clients",
        "name": "Name", "address": "Adresse", "interval": "Intervall", "last_seen": "Letzte Anfrage",
        "first_seen": "Zuerst gesehen", "all": "Alle", "active": "Aktiv", "inactive": "Inaktiv",
        "search": "Suchen", "no_clients": "Noch kein Client hat nach der Zeit gefragt.",
        "no_match": "Kein Client passt.", "ago": "vor {}", "unknown": "–",
        "active_info": "Aktiv heißt: Die letzte Anfrage liegt weniger als drei Intervalle des Clients und {} zurück.",
        "counted": "Anfragen und Verworfen zählen seit „Zuerst gesehen“, frühestens seit {}.",
        "missing": "Kein Client fragt mehr an, letzte Anfrage {}", "expected": "erwartet alle {}",
        "edit": "Bearbeiten", "edit_title": "Client bearbeiten", "alias": "Name", "note": "Notiz",
        "save": "Speichern", "cancel": "Abbrechen", "forget": "Vergessen",
        "forget_confirm": "Diesen Client vergessen? Mit seiner nächsten Anfrage erscheint er wieder und zählt ab dann neu.",
        "save_failed": "Speichern fehlgeschlagen.",
        "chrony_down": "chronyc antwortet nicht.", "loading": "Chrony wird gelesen …", "days": "d", "hours": "h", "minutes": "min", "seconds": "s",
        "states": {"*": "ausgewählt", "+": "kombiniert", "-": "nicht kombiniert", "?": "nicht erreichbar",
                   "x": "falsche Zeit", "~": "zu schwankend"},
        "overview": "Übersicht", "upstream": "Upstream", "clients_summary": "{} aktiv, {} inaktiv",
        "http_requests": "HTTP-Anfragen", "all_requests": "Anfragen", "protocol": "Protokoll",
        "now": "Aktuelle Zeit", "device_clock": "Uhr dieses Geräts", "ahead": "geht {} vor", "behind": "geht {} nach",
        "in_step": "geht genau", "measured": "über Home Assistant gemessen auf ± {}", "measuring": "Wird gemessen …",
        "no_time": "chrony gibt gerade keine Zeit aus.",
        "block": "Blockieren", "unblock": "Freigeben", "blocked": "Blockiert",
        "block_confirm": "Diesen Client blockieren? Er bekommt keine Zeit mehr, weder per NTP noch per HTTP, bis du ihn freigibst.",
        "blocked_note": "Blockiert: bekommt keine Zeit",
    },
}

# Material Design Icons (Apache 2.0), as Home Assistant draws them
ICONS = {
    "overview": "M19,5V7H15V5H19M9,5V11H5V5H9M19,13V19H15V13H19M9,17V19H5V17H9M21,3H13V9H21V3M11,3H3V13H11V3M21,11H13V21H21V11M11,15H3V21H11V15Z",
    "upstream": "M13,19H14A1,1 0 0,1 15,20H22V22H15A1,1 0 0,1 14,23H10A1,1 0 0,1 9,22H2V20H9A1,1 0 0,1 10,19H11V17H4A1,1 0 0,1 3,16V12A1,1 0 0,1 4,11H20A1,1 0 0,1 21,12V16A1,1 0 0,1 20,17H13V19M4,3H20A1,1 0 0,1 21,4V8A1,1 0 0,1 20,9H4A1,1 0 0,1 3,8V4A1,1 0 0,1 4,3M9,7H10V5H9V7M9,15H10V13H9V15M5,5V7H7V5H5M5,13V15H7V13H5Z",
    "clients": "M3 6H21V4H3C1.9 4 1 4.9 1 6V18C1 19.1 1.9 20 3 20H7V18H3V6M13 12H9V13.78C8.39 14.33 8 15.11 8 16C8 16.89 8.39 17.67 9 18.22V20H13V18.22C13.61 17.67 14 16.88 14 16S13.61 14.33 13 13.78V12M11 17.5C10.17 17.5 9.5 16.83 9.5 16S10.17 14.5 11 14.5 12.5 15.17 12.5 16 11.83 17.5 11 17.5M22 8H16C15.5 8 15 8.5 15 9V19C15 19.5 15.5 20 16 20H22C22.5 20 23 19.5 23 19V9C23 8.5 22.5 8 22 8M21 18H17V10H21V18Z",
    "chevron": "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
    "settings": "M12,8A4,4 0 0,1 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12A4,4 0 0,1 12,8M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10M10,22C9.75,22 9.54,21.82 9.5,21.58L9.13,18.93C8.5,18.68 7.96,18.34 7.44,17.94L4.95,18.95C4.73,19.03 4.46,18.95 4.34,18.73L2.34,15.27C2.21,15.05 2.27,14.78 2.46,14.63L4.57,12.97L4.5,12L4.57,11L2.46,9.37C2.27,9.22 2.21,8.95 2.34,8.73L4.34,5.27C4.46,5.05 4.73,4.96 4.95,5.05L7.44,6.05C7.96,5.66 8.5,5.32 9.13,5.07L9.5,2.42C9.54,2.18 9.75,2 10,2H14C14.25,2 14.46,2.18 14.5,2.42L14.87,5.07C15.5,5.32 16.04,5.66 16.56,6.05L19.05,5.05C19.27,4.96 19.54,5.05 19.66,5.27L21.66,8.73C21.79,8.95 21.73,9.22 21.54,9.37L19.43,11L19.5,12L19.43,13L21.54,14.63C21.73,14.78 21.79,15.05 21.66,15.27L19.66,18.73C19.54,18.95 19.27,19.04 19.05,18.95L16.56,17.95C16.04,18.34 15.5,18.68 14.87,18.93L14.5,21.58C14.46,21.82 14.25,22 14,22H10M11.25,4L10.88,6.61C9.68,6.86 8.62,7.5 7.85,8.39L5.44,7.35L4.69,8.65L6.8,10.2C6.4,11.37 6.4,12.64 6.8,13.8L4.68,15.36L5.43,16.66L7.86,15.62C8.63,16.5 9.68,17.14 10.87,17.38L11.24,20H12.76L13.13,17.39C14.32,17.14 15.37,16.5 16.14,15.62L18.57,16.66L19.32,15.36L17.2,13.81C17.6,12.64 17.6,11.37 17.2,10.2L19.31,8.65L18.56,7.35L16.15,8.39C15.38,7.5 14.32,6.86 13.12,6.62L12.75,4H11.25Z",
    "ok": "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z",
    "alert": "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    "refresh": "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
    "pencil": "M20.71,7.04C21.1,6.65 21.1,6 20.71,5.63L18.37,3.29C18,2.9 17.35,2.9 16.96,3.29L15.12,5.12L18.87,8.87M3,17.25V21H6.75L17.81,9.93L14.06,6.18L3,17.25Z",
    "search": "M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z",
}


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="{ICONS[name]}"/></svg>'


@functools.cache
def stylesheet() -> str:
    """The stylesheet all the apps share (ha.css), then this page's own rules."""
    try:
        return STYLESHEET.read_text() + APP_STYLE
    except OSError as err:
        LOGGER.warning("Cannot read the stylesheet: %s", err)
        return APP_STYLE


# The app's store icon at the left of the header, copied into the image next
# to the stylesheet; the page links it as icon.png, relative to the ingress path
APP_ICON_FILE = STYLESHEET.with_name("icon.png")


def app_icon() -> bytes | None:
    try:
        return APP_ICON_FILE.read_bytes()
    except OSError:
        LOGGER.warning("Icon %s missing", APP_ICON_FILE)
        return None


APP_ICON = app_icon()
APP_ICON_TAG = '<img class="app-icon" src="icon.png" alt="" width="24" height="24">'

APP_STYLE = """
header > .app-icon { flex: none; width: 24px; height: 24px; margin-right: 12px; }
header > h1 { flex: none; }
/* The views as tabs in the header row, like Home Assistant's own (and Network
   Plus); on phone width a bar at the bottom with icons, as in its app */
header > nav.tabs { display: flex; align-self: stretch; min-width: 0; margin-left: 24px; overflow-x: auto;
  scrollbar-width: none; }
nav.tabs > a { display: flex; align-items: center; justify-content: center; padding: 0 20px; position: relative;
  color: var(--sidebar-text-color); font-weight: var(--ha-font-weight-medium); white-space: nowrap; opacity: .8; }
nav.tabs > a:hover { background: var(--ha-hover); }
nav.tabs > a.active { color: var(--primary-color); opacity: 1; }
nav.tabs > a.active::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 2px;
  background: var(--primary-color); }
nav.tabs > a > svg { display: none; }
@media (max-width: 600px) {
  header > nav.tabs { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2; margin: 0; overflow: hidden;
    background: var(--sidebar-background-color); border-top: 1px solid var(--divider-color);
    padding-bottom: env(safe-area-inset-bottom); }
  nav.tabs > a { flex: 1 1 0; flex-direction: column; gap: 2px; height: 56px; padding: 6px 2px 4px; min-width: 0;
    font-size: 11px; }
  nav.tabs > a > span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
  nav.tabs > a > svg { display: block; padding: 2px 12px; border-radius: 14px; box-sizing: content-box; }
  nav.tabs > a.active > svg { background: color-mix(in srgb, var(--primary-color) 15%, transparent); }
  nav.tabs > a.active::after { display: none; }
  body:has(> header > nav.tabs) { padding-bottom: calc(56px + env(safe-area-inset-bottom)); }
}
a.state { margin: 0 -8px 8px; padding: 4px 8px; border-radius: 8px; }
a.state:hover { background: var(--ha-hover); }
a.state > svg.go { margin-left: auto; color: var(--secondary-text-color); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--disabled-text-color); }
.dot.on { background: var(--success-color); }
.sub.memo { max-width: 320px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.list td.act { width: 56px; padding-right: 8px; }
.list td.proto { white-space: nowrap; }
.dot.blocked { background: var(--error-color); }
.sub.blocked { color: var(--error-color); }
.clock { margin-bottom: 8px; }
.clock .time { font-size: 40px; line-height: 48px; font-variant-numeric: tabular-nums; letter-spacing: .5px; }
.clock .time .ms { font-size: 24px; color: var(--secondary-text-color); }
"""

# Takes Home Assistant's theme from the page around the ingress frame (same
# origin) and follows it; sorts and filters the clients; refreshes the page
THEME_VARS = ("primary-background-color", "secondary-background-color", "card-background-color",
              "primary-text-color", "secondary-text-color", "divider-color", "primary-color", "success-color",
              "warning-color", "error-color", "disabled-text-color", "sidebar-background-color",
              "sidebar-text-color", "sidebar-icon-color", "app-header-border-bottom", "ha-card-border-radius",
              "ha-card-border-width", "ha-card-border-color", "ha-card-box-shadow", "ha-font-family-body",
              "text-primary-color", "info-color",
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
  // Swaps in a fresh copy of the page; now asks the server to read chrony at once
  let busy = false;
  const update = async (now) => {
    if (busy) return;
    busy = true;
    const button = document.querySelector("#refresh");
    if (now && button) button.classList.add("busy");
    try {
      const url = new URL(location.href);
      if (now) url.searchParams.set("refresh", "1");
      const page = await fetch(url, { cache: "no-store" });
      if (!page.ok) return;
      const doc = new DOMParser().parseFromString(await page.text(), "text/html");
      const fresh = doc.querySelector("main");
      const q = document.querySelector("#q"), typing = q && document.activeElement === q;
      if (typing) fresh.querySelector("#q").replaceWith(q);
      document.querySelector("main").replaceWith(fresh);
      const stamp = document.querySelector("header .stamp"), freshStamp = doc.querySelector("header .stamp");
      if (stamp && freshStamp) stamp.replaceWith(freshStamp);
      apply();
      if (typing) q.focus();
    } catch (e) {
    } finally {
      busy = false;
      if (button) button.classList.remove("busy");
    }
  };
  document.addEventListener("click", (e) => {
    if (e.target.closest("#refresh")) { e.preventDefault(); update(true); }
  });
  // A client's name and note, and forgetting it. JSON only: a form of another
  // site cannot send it without asking first
  const dialog = () => document.querySelector("#edit");
  const field = (id) => document.querySelector("#edit-" + id);
  const count = () => { field("count").textContent = field("note").value.length + "/" + field("note").maxLength; };
  const change = async (body) => {
    const d = dialog();
    try {
      const url = new URL(location.href);
      url.search = "";
      const answer = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ ip: d.dataset.ip, ...body }) });
      if (!answer.ok) throw new Error(answer.status);
      d.close();
      update(true);
    } catch (err) {
      field("error").textContent = d.dataset.failed;
    }
  };
  document.addEventListener("click", (e) => {
    const edit = e.target.closest("button.edit"), d = dialog();
    if (edit) {
      const row = edit.closest("tr");
      d.dataset.ip = row.dataset.addr;
      field("addr").textContent = row.dataset.addr;
      field("name").value = row.dataset.alias;
      field("name").placeholder = row.dataset.dns || row.dataset.addr;
      field("note").value = row.dataset.note;
      field("error").textContent = "";
      const blocked = row.dataset.blocked === "1", block = field("block");
      d.dataset.blocked = blocked ? "1" : "";
      block.textContent = blocked ? block.dataset.unblock : block.dataset.block;
      // A blocked client stays listed until it is unblocked
      field("forget").style.display = blocked ? "none" : "";
      count();
      d.showModal();
      field("name").focus();
    }
    if (e.target.closest("#edit-forget") && confirm(field("forget").dataset.confirm)) change({ action: "forget" });
    if (e.target.closest("#edit-block")) {
      if (d.dataset.blocked === "1") change({ action: "unblock" });
      else if (confirm(field("block").dataset.confirm)) change({ action: "block" });
    }
    if (e.target.closest("#edit-cancel")) d.close();
  });
  // Save, also with Enter in the name
  document.addEventListener("submit", (e) => {
    if (!e.target.closest("#edit")) return;
    e.preventDefault();
    change({ action: "edit", name: field("name").value, note: field("note").value });
  });
  document.addEventListener("input", (e) => { if (e.target.id === "edit-note") count(); });
  setInterval(() => { if (!document.hidden) update(false); }, REFRESH * 1000);
  // The current time. The browser's clock is measured against the server's
  // (chrony's time) through the ingress: of several round trips the shortest
  // gives the offset, half of it the error. The clock then ticks with the
  // browser's monotonic clock plus that offset, in Home Assistant's time zone.
  const tick = { offset: null, error: null, off: false, zone: undefined, shown: "" };
  try {
    const ha = window.parent.document.querySelector("home-assistant");
    if (ha && ha.hass) tick.zone = ha.hass.config.time_zone;
  } catch (e) {}
  const mono = () => performance.timeOrigin + performance.now();
  let measuring = false;
  const measure = async () => {
    if (measuring || document.hidden || !document.querySelector("#clock")) return;
    measuring = true;
    try {
      const url = new URL("now", location.href);
      let best = null;
      for (let i = 0; i < SAMPLES; i++) {
        const t0 = mono();
        const answer = await fetch(url, { cache: "no-store" });
        if (!answer.ok) return;
        const data = await answer.json();
        const t1 = mono();
        if (data.time === null) { tick.off = true; return; }
        const sample = { rtt: t1 - t0, offset: data.time * 1000 - (t0 + t1) / 2 };
        if (!best || sample.rtt < best.rtt) best = sample;
      }
      tick.off = false;
      tick.offset = best.offset;
      tick.error = best.rtt / 2;
    } catch (e) {
    } finally {
      measuring = false;
    }
  };
  const formats = {};
  const format = (lang) => formats[lang] || (formats[lang] = {
    time: new Intl.DateTimeFormat(lang, { hour: "2-digit", minute: "2-digit", second: "2-digit",
                                          fractionalSecondDigits: 3, hourCycle: "h23", timeZone: tick.zone }),
    date: new Intl.DateTimeFormat(lang, { weekday: "long", day: "numeric", month: "long", year: "numeric",
                                          timeZone: tick.zone }),
    zone: new Intl.DateTimeFormat(lang, { timeZoneName: "short", timeZone: tick.zone }),
    number: new Intl.NumberFormat(lang, { maximumFractionDigits: 1 }),
  });
  const span = (ms, f) => Math.abs(ms) < 1000 ? f.number.format(Math.abs(ms)) + " ms"
                                              : f.number.format(Math.abs(ms) / 1000) + " s";
  const draw = () => {
    const box = document.querySelector("#clock");
    if (box) {
      const f = format(box.dataset.lang);
      const time = document.querySelector("#clock-time"), date = document.querySelector("#clock-date");
      const offset = document.querySelector("#clock-offset"), error = document.querySelector("#clock-error");
      if (tick.off) {
        time.textContent = "--:--:--";
        offset.textContent = box.dataset.noTime;
        error.textContent = "";
      } else if (tick.offset !== null) {
        const now = new Date(mono() + tick.offset);
        const parts = f.time.formatToParts(now);
        const frac = parts.find((p) => p.type === "fractionalSecond");
        const main = parts.filter((p) => p.type !== "fractionalSecond").map((p) => p.value).join("").replace(/[.,]$/, "");
        time.innerHTML = "";
        time.append(main);
        const ms = document.createElement("span");
        ms.className = "ms";
        ms.textContent = "." + (frac ? frac.value : "000");
        time.append(ms);
        const zone = f.zone.formatToParts(now).find((p) => p.type === "timeZoneName");
        const day = f.date.format(now) + (zone ? " · " + zone.value : "");
        if (date.textContent !== day) date.textContent = day;
        // Positive: this device is ahead of the server
        const ahead = -tick.offset;
        const text = Math.abs(ahead) <= tick.error ? box.dataset.inStep
          : (ahead > 0 ? box.dataset.ahead : box.dataset.behind).replace("{}", span(ahead, f));
        if (offset.textContent !== text) offset.textContent = text;
        const bound = box.dataset.measured.replace("{}", span(tick.error, f));
        if (error.textContent !== bound) error.textContent = bound;
      }
    }
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
  document.addEventListener("DOMContentLoaded", measure);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) measure(); });
  setInterval(measure, MEASURE_EVERY * 1000);
})();""".replace("NAMES", json.dumps(THEME_VARS)).replace("REFRESH", str(REFRESH)).replace(
    "MEASURE_EVERY", str(MEASURE_EVERY)).replace("SAMPLES", str(SAMPLES))


def shared_script() -> str:
    """The script all the apps share (ha-lang.js), which runs before this page's own."""
    try:
        return SHARED_SCRIPT_FILE.read_text(encoding="utf-8")
    except OSError as err:
        LOGGER.warning("Cannot read the shared script: %s", err)
        return ""


SCRIPT = shared_script() + SCRIPT


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


def chrony_run() -> str | None:
    """Which chronyd is running: boot, process and its start time. A new one
    starts its counters at zero."""
    try:
        pid = PIDFILE.read_text().strip()
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError):
        return None
    return f"{boot}:{pid}:{start}"


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
            "clients": clients, "run": chrony_run()}


def sync_problem(tracking: dict) -> bool:
    """The same rule as the Sync problem sensor."""
    return tracking["stratum"] >= 16 or tracking["leap"] != "Normal" or tracking["refid"] == LOCAL_CLOCK


def expected(interval: int | None) -> float:
    """Seconds after its last request within which a client counts as active."""
    return 3 * 2.0 ** (DEFAULT_INTERVAL if interval is None else interval) + GRACE


def interval_of(entry: dict) -> int | None:
    """A client's interval as a power of two: chrony's for NTP, else the one
    the HTTP time service saw."""
    if entry.get("interval") is not None:
        return entry["interval"]
    return (entry.get("http") or {}).get("interval")


def address(ip: str) -> str:
    """An address in its one written form; ValueError for anything else."""
    return str(ipaddress.ip_address(ip))


def control(*args: str) -> None:
    """A command to chronyd over its command socket, which admits this user."""
    subprocess.run(["chronyc", "-n", *args], capture_output=True, text=True, timeout=10, check=True)


def read_allowed(path: Path) -> list:
    try:
        lines = path.read_text().split()
    except OSError:
        return []
    networks = []
    for line in lines:
        with contextlib.suppress(ValueError):
            networks.append(ipaddress.ip_network(line, strict=False))
    return networks


def read_http(path: Path) -> dict | None:
    """The HTTP time service's client log; None before it wrote one."""
    try:
        log = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    clients = {}
    for ip, c in log["clients"].items():
        if ip in LOCALHOST:
            continue
        clients[str(ip)] = {"requests": int(c["requests"]), "dropped": int(c["dropped"]), "last": float(c["last"]),
                            "interval": None if c.get("interval") is None else int(c["interval"])}
    return {"run": log.get("run"), "requests": int(log["requests"]), "dropped": int(log["dropped"]),
            "clients": clients}


def clean(value, limit: int) -> str:
    """A name or note as one line of plain text with single spaces."""
    if not isinstance(value, str):
        raise ValueError("text expected")
    text = " ".join("".join(" " if unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn") or ch in BIDI else ch
                            for ch in value).split())
    if len(text) > limit:
        raise ValueError(f"more than {limit} characters")
    return text


# --- Names and the client store ------------------------------------------------------
class Names:
    """Host names of client addresses by reverse DNS, as the router hands them out.

    A lookup without an answer can take seconds until the resolver gives up,
    so the lookups run in a thread of their own: get() answers at once with
    what is known, and an address asked for the first time has its name with
    a later read. A resolver given for the tests answers at once instead.
    """

    def __init__(self, resolve=None) -> None:
        self._background = resolve is None
        self._resolve = resolve or self._lookup
        self._known: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._waiting: set[str] = set()
        self._queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        if self._background:
            threading.Thread(target=self._work, daemon=True).start()

    @staticmethod
    def _lookup(ip: str) -> str:
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            return ""
        # Without an entry musl answers with the address itself
        return "" if name == ip else name

    def get(self, ip: str, now: float) -> str:
        with self._lock:
            known = self._known.get(ip)
            if known is not None and now - known[0] <= NAME_TTL:
                return known[1]
            if self._background:
                # Bounded: what does not fit is asked for with a later read
                if ip not in self._waiting and len(self._waiting) < LOOKUPS_MAX:
                    self._waiting.add(ip)
                    self._queue.put(ip)
                return known[1] if known else ""
        name = self._resolve(ip)
        self._keep(ip, now, name)
        return name

    def _keep(self, ip: str, now: float, name: str) -> None:
        with self._lock:
            self._known[ip] = (now, name)
            self._waiting.discard(ip)
            if len(self._known) > NAMES_MAX:
                # The oldest answers go first
                for old in sorted(self._known, key=lambda k: self._known[k][0])[: len(self._known) - NAMES_MAX]:
                    del self._known[old]

    def _work(self) -> None:
        while True:
            ip = self._queue.get()
            self._keep(ip, time.time(), self._resolve(ip))


class Clients:
    """When each client was first and last seen, its interval, name and note,
    and the request totals, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self.totals: dict = {}
        # Forgotten clients chrony, or the HTTP time service, still knows, with their counters then
        self.forgotten: dict[str, dict] = {}
        self.forgotten_http: dict[str, dict] = {}
        self._saved = 0.0
        self._warned = 0.0
        try:
            stored = json.loads(path.read_text())
            self.data = dict(stored["clients"])
            self.totals = dict(stored.get("totals") or {})
            self.forgotten = dict(stored.get("forgotten") or {})
            self.forgotten_http = dict(stored.get("forgotten_http") or {})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
            # Kept aside instead of being overwritten with the next save
            broken = path.with_name(path.name + ".broken")
            LOGGER.warning("Cannot read %s (%s); starting with no clients, the file is kept as %s",
                           path, err, broken.name)
            self.data, self.totals, self.forgotten, self.forgotten_http = {}, {}, {}, {}
            try:
                os.replace(path, broken)
            except OSError as move:
                LOGGER.warning("Cannot keep %s aside: %s", path, move)

    @staticmethod
    def add(entry: dict, values: dict, restarted: bool) -> None:
        """Takes chrony's counters into an entry. They only grow while one
        chronyd runs; after a new chronyd, or when a counter got smaller
        (chrony dropped the client from its log), what was counted before
        goes into the base. A counter seen for the first time is taken as is."""
        first = any(key not in entry for key in COUNTERS)
        for key in COUNTERS:
            entry.setdefault("base_" + key, 0)
        if not first and (restarted or any(values[key] < entry[key] for key in COUNTERS)):
            for key in COUNTERS:
                entry["base_" + key] += entry[key]
        for key in COUNTERS:
            entry[key] = values[key]

    @staticmethod
    def active(entry: dict, now: float) -> bool:
        return now - entry["last_seen"] < expected(interval_of(entry))

    def requests_missing(self, now: float) -> bool:
        """Clients asked in the last RECENT, but none within its usual time."""
        recent = [e for e in self.data.values() if now - e["last_seen"] < RECENT and not e.get("blocked")]
        return bool(recent) and not any(self.active(e, now) for e in recent)

    def edit(self, ip: str, alias: str, note: str, now: float) -> bool:
        entry = self.data.get(ip)
        if entry is None:
            return False
        for key, value in (("alias", alias), ("note", note)):
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)
        self.save(now)
        return True

    def forget(self, ip: str, live: dict | None, now: float, live_http: dict | None = None) -> bool:
        """Drops a client. While chrony or the HTTP time service still knows
        it, it comes back only with a new request, counted from then on."""
        if ip not in self.data or self.data[ip].get("blocked"):
            return False
        del self.data[ip]
        for counters, forgotten in ((live, self.forgotten), (live_http, self.forgotten_http)):
            if counters is not None:
                forgotten[ip] = {key: counters[key] for key in COUNTERS}
        self.save(now)
        return True

    def _take(self, ip: str, values: dict, seen: int, forgotten: dict, http: bool) -> tuple[dict, dict] | None:
        """A client's entry and the part that holds the counters of one
        protocol: the entry itself for NTP, its "http" for HTTP. None while
        the client is forgotten and has not asked again over this protocol."""
        gone = forgotten.get(ip)
        if gone is not None:
            if all(values[key] == gone[key] for key in COUNTERS):
                return None
            del forgotten[ip]
        entry = self.data.get(ip)
        if entry is None:
            entry = self.data[ip] = {"first_seen": seen, "last_seen": seen, "name": ""}
        target = entry.setdefault("http", {}) if http else entry
        if gone is not None and "requests" not in target and all(values[key] >= gone[key] for key in COUNTERS):
            # Only what came after it was forgotten counts
            for key in COUNTERS:
                target["base_" + key] = -gone[key]
        return entry, target

    @staticmethod
    def sum(entry: dict, key: str) -> int:
        return entry["base_" + key] + entry[key]

    @classmethod
    def requests(cls, entry: dict) -> int | None:
        """A client's requests over NTP and HTTP, None before any was counted."""
        parts = [cls.sum(part, "requests") for part in (entry, entry.get("http") or {}) if "requests" in part]
        return sum(parts) if parts else None

    @classmethod
    def dropped(cls, entry: dict) -> int | None:
        parts = [cls.sum(part, "dropped") for part in (entry, entry.get("http") or {}) if "dropped" in part]
        return sum(parts) if parts else None

    @staticmethod
    def protocols(entry: dict) -> list[str]:
        """The protocols a client asked over."""
        return [name for name, part in (("NTP", entry), ("HTTP", entry.get("http") or {})) if "requests" in part]

    def total(self, key: str) -> int:
        return self.sum(self.totals, key)

    def count(self, chrony: dict, now: float) -> bool:
        """Takes the server's counters into the totals; True after a new chronyd."""
        t = self.totals
        t.setdefault("since", round(now))
        run = chrony.get("run")
        # Without a stored run (first start, or a store of 1.5) the counters are taken as is
        restarted = run is not None and t.get("run") not in (None, run)
        if run is not None:
            t["run"] = run
        self.add(t, chrony, restarted)
        return restarted

    def update(self, clients: list[dict], names: dict[str, str], now: float, restarted: bool = False) -> None:
        live = {c["ip"] for c in clients}
        if restarted:
            # The new chronyd starts every client at zero, also those it has not heard from yet
            for ip, entry in self.data.items():
                if ip not in live and "requests" in entry:
                    self.add(entry, {key: 0 for key in COUNTERS}, True)
        # A forgotten client chrony no longer knows is new with its next request
        for ip in [ip for ip in self.forgotten if restarted or ip not in live]:
            del self.forgotten[ip]
        # A client without an NTP request only used chronyc
        for c in (c for c in clients if c["last"] is not None):
            seen = round(now - c["last"])
            taken = self._take(c["ip"], c, seen, self.forgotten, http=False)
            if taken is None:
                continue
            entry = taken[0]
            self.add(entry, c, restarted)
            entry["last_seen"] = max(entry["last_seen"], seen)
            entry["first_seen"] = min(entry["first_seen"], seen)
            entry["name"] = names.get(c["ip"]) or entry["name"]
            if c["interval"] is not None:
                entry["interval"] = c["interval"]
        self.tidy(now)

    def update_http(self, http: dict, names: dict[str, str], now: float) -> None:
        """Takes in the HTTP time service's client log, like chrony's."""
        t = self.totals.setdefault("http", {})
        t.setdefault("since", round(now))
        run = http.get("run")
        restarted = run is not None and t.get("run") not in (None, run)
        if run is not None:
            t["run"] = run
        self.add(t, http, restarted)
        live = http["clients"]
        if restarted:
            for ip, entry in self.data.items():
                part = entry.get("http")
                if ip not in live and part and "requests" in part:
                    self.add(part, {key: 0 for key in COUNTERS}, True)
        for ip in [ip for ip in self.forgotten_http if restarted or ip not in live]:
            del self.forgotten_http[ip]
        for ip, c in live.items():
            seen = round(min(c["last"], now))
            taken = self._take(ip, c, seen, self.forgotten_http, http=True)
            if taken is None:
                continue
            entry, part = taken
            self.add(part, c, restarted)
            entry["last_seen"] = max(entry["last_seen"], seen)
            entry["first_seen"] = min(entry["first_seen"], seen)
            entry["name"] = names.get(ip) or entry["name"]
            if c["interval"] is not None:
                part["interval"] = c["interval"]

    def tidy(self, now: float) -> None:
        """Drops clients away too long and beyond the limit; saves now and then."""
        # A client with a name or note given on the page stays until it is forgotten there
        for ip in [ip for ip, e in self.data.items()
                   if now - e["last_seen"] > FORGET_AFTER and not self.kept(e)]:
            del self.data[ip]
        self._limit(now)
        if now - self._saved >= SAVE_EVERY:
            self.save(now)

    @staticmethod
    def kept(entry: dict) -> bool:
        """A client with a name or note given on the page stays until it is
        forgotten there, a blocked one until it is unblocked."""
        return bool(entry.get("alias") or entry.get("note") or entry.get("blocked"))

    def blocked(self) -> list[str]:
        return sorted(ip for ip, e in self.data.items() if e.get("blocked"))

    def _limit(self, now: float) -> None:
        """At most MAX_CLIENTS: beyond that the clients that asked only once
        go first, then those away longest; named ones stay."""
        excess = len(self.data) - MAX_CLIENTS
        if excess <= 0:
            return
        drop = sorted((ip for ip, e in self.data.items() if not self.kept(e)),
                      key=lambda ip: ((self.requests(self.data[ip]) or 0) > 1, self.data[ip]["last_seen"]))[:excess]
        for ip in drop:
            del self.data[ip]
        if now - self._warned >= WARN_EVERY:
            self._warned = now
            LOGGER.warning("More than %d clients; dropped %d that asked least recently. Limit who may "
                           "take time with the option allowed_networks", MAX_CLIENTS, len(drop))

    def save(self, now: float) -> None:
        """Writes the store whole or not at all; the caller holds the state's lock."""
        tmp = None
        try:
            text = json.dumps({"clients": self.data, "totals": self.totals, "forgotten": self.forgotten,
                               "forgotten_http": self.forgotten_http}, sort_keys=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
            tmp = None
            self._saved = now
        except (OSError, RuntimeError, TypeError, ValueError) as err:
            LOGGER.warning("Cannot save %s: %s", self.path, err)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


class State:
    """The latest read of chrony, renewed in the background every POLL seconds."""

    def __init__(self, clients: Clients, names: Names | None = None, read=read_chrony, clock=time.time,
                 status: Path | None = None, http_log: Path | None = None, blocked: Path | None = None,
                 allowed: Path | None = None, command=control) -> None:
        self.clients = clients
        self._blocked = blocked
        self._allowed = allowed
        self._command = command
        # The chronyd that knows the blocked addresses
        self._denied_run: str | None = None
        self.names = names or Names()
        self._read = read
        self._http_log = http_log
        self.http: dict | None = None
        self._clock = clock
        self._status = status
        self.lock = threading.Lock()
        self.chrony: dict | None = None
        self.at = 0.0
        # One read of chronyc at a time: the background loop or a refresh button
        self._reading = threading.Lock()
        # What failed last, so the log tells once per outage
        self._failing: set[str] = set()

    def refresh(self) -> None:
        with self._reading:
            self._refresh()

    def _refresh(self) -> None:
        now = self._clock()
        try:
            chrony = self._read()
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as err:
            self._failed("chronyc", "chronyc does not answer (%s), retrying every %d s", err, POLL)
            chrony = None
        else:
            self._recovered("chronyc", "chronyc answers again")
        http = self._read_http()
        # Names known so far; new ones are looked up in the background
        names = {c["ip"]: self.names.get(c["ip"], now) for c in (chrony or {}).get("clients", [])
                 if c["last"] is not None}
        names.update({ip: self.names.get(ip, now) for ip in (http or {}).get("clients", {})})
        if chrony is not None:
            # chronyc's CSV output has addresses only, also for the upstream servers
            for source in chrony["sources"]:
                source["host"] = self.names.get(source["name"], now)
            refname = chrony["tracking"]["refname"]
            chrony["tracking"]["host"] = self.names.get(refname, now) if refname else ""
        with self.lock:
            self.chrony, self.at, self.http = chrony, now, http
            if http is not None:
                self.clients.update_http(http, names, now)
            if chrony is not None:
                restarted = self.clients.count(chrony, now)
                self.clients.update(chrony["clients"], names, now, restarted)
            elif http is not None:
                self.clients.tidy(now)
            # Judged from the kept clients also while chronyc fails: a chronyd
            # that does not answer serves nobody either
            missing = self.clients.requests_missing(now)
        self._write_status({"at": round(now), "requests_missing": missing})
        if chrony is not None and (chrony.get("run") is None or chrony.get("run") != self._denied_run):
            self._deny_all(chrony.get("run"))

    def _deny_all(self, run: str | None) -> None:
        """Gives a new chronyd the blocked addresses; it starts without them."""
        with self.lock:
            blocked = self.clients.blocked()
        try:
            for ip in blocked:
                self._command("deny", ip)
        except (OSError, subprocess.SubprocessError) as err:
            self._failed("deny", "Cannot block clients in chronyd (%s), retrying", err)
            return
        self._recovered("deny", "Blocked clients given to chronyd again")
        self._denied_run = run
        self._write_blocked(blocked)

    def _write_blocked(self, blocked: list[str]) -> None:
        if self._blocked is None:
            return
        tmp = self._blocked.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(blocked))
            os.replace(tmp, self._blocked)
        except OSError as err:
            self._failed("blocked", "Cannot write %s: %s", self._blocked, err)
        else:
            self._recovered("blocked", "%s written again", self._blocked)

    def block(self, ip: str, on: bool) -> int:
        """Blocks or unblocks a client in chronyd and for HTTP; the HTTP status."""
        with self.lock:
            entry = self.clients.data.get(ip)
            if entry is None:
                return 404
            if bool(entry.get("blocked")) == on:
                return 204
            if on and len(self.clients.blocked()) >= BLOCK_MAX:
                return 409
        try:
            if on:
                self._command("deny", ip)
            elif any(ipaddress.ip_address(ip) in n for n in read_allowed(self._allowed or ALLOWED)
                     if n.version == ipaddress.ip_address(ip).version):
                # chronyd has no way to drop a deny: an allow for the one address
                # undoes it, given only where the configuration serves it anyway
                self._command("allow", ip)
        except (OSError, subprocess.SubprocessError) as err:
            LOGGER.warning("Cannot %s %s in chronyd: %s", "block" if on else "unblock", ip, err)
            return 503
        with self.lock:
            if on:
                entry["blocked"] = True
            else:
                entry.pop("blocked", None)
            self.clients.save(self._clock())
            blocked = self.clients.blocked()
        self._write_blocked(blocked)
        LOGGER.info("%s %s", "Blocked" if on else "Unblocked", ip)
        return 204

    def _read_http(self) -> dict | None:
        if self._http_log is None:
            return None
        try:
            http = read_http(self._http_log)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
            self._failed("http", "Cannot read %s (%s)", self._http_log, err)
            return None
        self._recovered("http", "%s readable again", self._http_log)
        return http

    def now(self) -> dict:
        """chrony's time for the page's clock: the host clock plus chrony's
        correction of its latest read."""
        with self.lock:
            chrony = self.chrony
        tracking = (chrony or {}).get("tracking")
        if tracking is None or tracking["offset"] is None or (
                sync_problem(tracking) and tracking["refid"] != LOCAL_CLOCK):
            return {"time": None}
        return {"time": self._clock() + tracking["offset"], "stratum": tracking["stratum"]}

    def _write_status(self, status: dict) -> None:
        if self._status is None:
            return
        tmp = self._status.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(status))
            os.replace(tmp, self._status)
        except OSError as err:
            # Without it the sensor Requests missing is unknown
            self._failed("status", "Cannot write %s: %s", self._status, err)
        else:
            self._recovered("status", "%s written again", self._status)

    def _failed(self, what: str, message: str, *args) -> None:
        if what not in self._failing:
            self._failing.add(what)
            LOGGER.warning(message, *args)

    def _recovered(self, what: str, message: str, *args) -> None:
        if what in self._failing:
            self._failing.discard(what)
            LOGGER.info(message, *args)

    def change(self, body: dict) -> int:
        """A change sent by the page; the HTTP status of the answer."""
        ip, action = body.get("ip"), body.get("action")
        if not isinstance(ip, str):
            raise ValueError("address expected")
        if action in ("block", "unblock"):
            return self.block(address(ip), action == "block")
        now = self._clock()
        with self.lock:
            if action == "edit":
                done = self.clients.edit(ip, clean(body.get("name", ""), NAME_MAX),
                                         clean(body.get("note", ""), NOTE_MAX), now)
            elif action == "forget":
                if (self.clients.data.get(ip) or {}).get("blocked"):
                    return 409
                live = next((c for c in (self.chrony or {}).get("clients", []) if c["ip"] == ip), None)
                live_http = (self.http or {}).get("clients", {}).get(ip)
                done = self.clients.forget(ip, live, now, live_http)
            else:
                raise ValueError("unknown action")
        return 204 if done else 404

    def run(self) -> None:
        while True:
            self.refresh()
            time.sleep(POLL)


# --- Formatting --------------------------------------------------------------------------
def lang_of(cookie: str | None, accept: str | None = None) -> str:
    """German or English, as Home Assistant's interface shows it.

    The page script keeps Home Assistant's language in the cookie ha_lang
    (share/ha-app/ha-lang.js): German for "de", English for everything else.
    Only without that cookie, German if the browser puts German before English.
    """
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "ha_lang":
            return "de" if value.strip().lower() == "de" else "en"
    for part in (accept or "").split(","):
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

    def day(self, epoch: float) -> str:
        return time.strftime("%d.%m.%Y" if self.lang == "de" else "%Y-%m-%d", time.localtime(epoch))

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


def status_card(f: Format, chrony: dict, clients: Clients, now: float, http: dict | None = None) -> str:
    t, tr = f.t, chrony["tracking"]
    reference = tr.get("host") or tr["refname"] or tr["refid"]
    go = icon("chevron", "go")
    if not sync_problem(tr):
        state = f'<a class="state ok" href="?tab=upstream">{icon("ok")}{esc(t["synced"].format(reference))}{go}</a>'
    else:
        # Before its first sync chrony reports stratum 0
        unsynced = tr["stratum"] >= 16 or tr["stratum"] == 0 or tr["leap"] == "Not synchronised"
        reason = (t["local"] if tr["refid"] == LOCAL_CLOCK else
                  t["unsynced"] if unsynced else t["leap"].format(tr["leap"]))
        state = f'<a class="state bad" href="?tab=upstream">{icon("alert")}{esc(reason)}{go}</a>'
    if clients.requests_missing(now):
        last = max(e["last_seen"] for e in clients.data.values())
        state += (f'<a class="state bad" href="?tab=clients">{icon("alert")}'
                  f'{esc(t["missing"].format(f.ago(now - last)))}{go}</a>')
    else:
        active = sum(Clients.active(e, now) and not e.get("blocked") for e in clients.data.values())
        state += (f'<a class="state muted" href="?tab=clients">{icon("clients")}{esc(t["clients"])}: '
                  f'{esc(t["clients_summary"].format(active, len(clients.data) - active))}{go}</a>')
    since = t["since"].format(f.day(clients.totals["since"]))
    last_sync = (f'{esc(f.ago(now - tr["reftime"]))}<span class="sub">{esc(f.when(tr["reftime"]))}</span>'
                 if tr["reftime"] else t["unknown"])
    return card(t["status"], state + kv([
        (t["reference"], esc(reference)),
        (t["stratum"], esc(tr["stratum"])),
        (t["last_sync"], last_sync),
        (t["offset"], esc(f.ms(tr["offset"]))),
        (t["requests"], f'{esc(f.int(clients.total("requests")))}<span class="sub">{esc(since)} · '
                        f'{esc(t["since_start"].format(f.int(chrony["requests"])))}</span>'),
        (t["dropped"], f'{esc(f.int(clients.total("dropped")))}<span class="sub">{esc(since)} · '
                       f'{esc(t["since_start"].format(f.int(chrony["dropped"])))}</span>'),
    ] + http_rows(f, clients, http)))


def http_rows(f: Format, clients: Clients, http: dict | None) -> list[tuple[str, str]]:
    """Requests over HTTP, once the HTTP time service has written its log."""
    t, totals = f.t, clients.totals.get("http")
    if not totals or "requests" not in totals:
        return []
    since = t["since"].format(f.day(totals["since"]))
    started = (f' · {esc(t["since_start"].format(f.int(http["requests"])))}' if http else "")
    dropped = Clients.sum(totals, "dropped")
    return [(t["http_requests"], f'{esc(f.int(Clients.sum(totals, "requests")))}<span class="sub">{esc(since)}{started}'
                                 + (f' · {esc(t["dropped"])} {esc(f.int(dropped))}' if dropped else "") + "</span>")]


def clock_card(f: Format) -> str:
    """The current time, ticking in the browser: the script measures the
    browser's clock against the server's several times through the ingress
    and keeps the measurement with the shortest round trip."""
    t = f.t
    return card(t["now"], (f'<div class="clock" id="clock" data-ahead="{esc(t["ahead"])}" data-behind="{esc(t["behind"])}"'
                           f' data-in-step="{esc(t["in_step"])}" data-measured="{esc(t["measured"])}"'
                           f' data-no-time="{esc(t["no_time"])}" data-lang="{f.lang}">'
                           f'<div class="time" id="clock-time">--:--:--<span class="ms">.---</span></div>'
                           f'<div class="sub" id="clock-date"></div></div>'
                           + kv([(t["device_clock"], f'<span id="clock-offset">{esc(t["measuring"])}</span>'
                                                     f'<span class="sub" id="clock-error"></span>')])))


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


def clients_card(f: Format, state: State, now: float) -> str:
    t = f.t
    live = {c["ip"]: c for c in (state.chrony or {}).get("clients", [])}
    rows = []
    active = blocked = 0
    for ip, entry in state.clients.data.items():
        c = live.get(ip)
        last_seen = entry["last_seen"]
        ago = now - last_seen
        is_blocked = bool(entry.get("blocked"))
        is_active = Clients.active(entry, now) and not is_blocked
        active += is_active
        blocked += is_blocked
        name, alias, note = entry["name"], entry.get("alias", ""), entry.get("note", "")
        shown = alias or short_name(name) or ip
        text = " ".join((alias, name, ip, note)).lower()
        last = f'{esc(f.ago(ago))}<span class="sub">{esc(f.when(last_seen))}</span>'
        all_requests, all_dropped = Clients.requests(entry), Clients.dropped(entry)
        requests = t["unknown"] if all_requests is None else esc(f.int(all_requests))
        dropped = t["unknown"] if all_dropped is None else esc(f.int(all_dropped))
        protocols = Clients.protocols(entry)
        text += " " + " ".join(protocols).lower()
        # chrony's interval while it knows the client, else the last one known
        exponent = c["interval"] if c and c["interval"] is not None else interval_of(entry)
        interval = esc(f.log2(exponent))
        sub = f'<span class="sub">{esc(ip)}</span>' if shown != ip else ""
        sub += f'<span class="sub memo" title="{esc(note)}">{esc(note)}</span>' if note else ""
        sub += f'<span class="sub blocked">{esc(t["blocked_note"])}</span>' if is_blocked else ""
        row_state = "blocked" if is_blocked else "active" if is_active else "inactive"
        dot = esc(t["blocked"] if is_blocked else
                  t[row_state] + " · " + t["expected"].format(f.log2(exponent)) if exponent is not None else t[row_state])
        rows.append(
            f'<tr data-name="{esc(shown.lower())}" data-text="{esc(text)}"'
            f' data-addr="{esc(ip)}" data-alias="{esc(alias)}" data-note="{esc(note)}" data-dns="{esc(short_name(name))}"'
            f' data-state="{row_state}" data-blocked="{"1" if is_blocked else ""}"'
            f' data-requests="{"" if all_requests is None else all_requests}"'
            f' data-dropped="{"" if all_dropped is None else all_dropped}"'
            f' data-protocol="{esc(" ".join(protocols).lower())}"'
            f' data-interval="{"" if exponent is None else exponent}"'
            f' data-last="{round(ago)}" data-first="{entry["first_seen"]}">'
            f'<td><span class="dot{" on" if is_active else " blocked" if is_blocked else ""}" title="{dot}"></span></td>'
            f'<td title="{esc(name or ip)}">{esc(shown)}{sub}</td>'
            f'<td class="proto">{esc(", ".join(protocols)) or t["unknown"]}</td>'
            f'<td class="n">{requests}</td><td class="n opt">{dropped}</td><td class="n opt">{interval}</td>'
            f'<td class="n">{last}</td><td class="n opt">{esc(f.when(entry["first_seen"]))}</td>'
            f'<td class="act"><button class="icon-button small edit" title="{esc(t["edit"])}"'
            f' aria-label="{esc(t["edit"])}">{icon("pencil")}</button></td></tr>')
    total = len(rows)
    empty = "" if rows else f'<tr><td colspan="9" class="muted">{esc(t["no_clients"])}</td></tr>'
    rows.append(f'<tr class="none" hidden><td colspan="9" class="muted">{esc(t["no_match"])}</td></tr>')
    chips = "".join(f'<button class="chip{" on" if key == "all" else ""}" data-show="{key}">{esc(t[key])} · {n}</button>'
                    for key, n in (("all", total), ("active", active), ("inactive", total - active - blocked))
                    + ((("blocked", blocked),) if blocked else ()))
    toolbar = (f'<div class="toolbar">{chips}<label class="search">{icon("search")}'
               f'<input id="q" type="search" placeholder="{esc(t["search"])}" autocomplete="off"></label></div>')
    head = (f'<thead><tr><th></th><th data-sort="name">{esc(t["name"])}</th>'
            f'<th data-sort="protocol">{esc(t["protocol"])}</th>'
            f'<th class="n" data-sort="requests">{esc(t["all_requests"])}</th>'
            f'<th class="n opt" data-sort="dropped">{esc(t["dropped"])}</th>'
            f'<th class="n opt" data-sort="interval">{esc(t["interval"])}</th>'
            f'<th class="n" data-sort="last">{esc(t["last_seen"])}</th>'
            f'<th class="n opt" data-sort="first">{esc(t["first_seen"])}</th><th></th></tr></thead>')
    since = state.clients.totals.get("since")
    info = t["active_info"].format(f.duration(GRACE)) + (" " + t["counted"].format(f.day(since)) if since else "")
    note = f'<div class="note">{esc(info)}</div>'
    return card(t["clients"], toolbar + f'<div class="scroll"><table class="list" id="clients">{head}'
                f'<tbody>{"".join(rows)}{empty}</tbody></table></div>' + note, padded=False)


def edit_dialog(t: dict) -> str:
    """One dialog for every client; the script fills it from the row."""
    return (f'<dialog id="edit" data-failed="{esc(t["save_failed"])}"><form>'
            f'<h2>{esc(t["edit_title"])}</h2><div class="muted" id="edit-addr"></div>'
            f'<label class="field"><span>{esc(t["alias"])}</span>'
            f'<input id="edit-name" maxlength="{NAME_MAX}" autocomplete="off"></label>'
            f'<label class="field"><span>{esc(t["note"])}</span>'
            f'<textarea id="edit-note" maxlength="{NOTE_MAX}" rows="3"></textarea>'
            f'<span class="count" id="edit-count"></span></label><div class="field-error" id="edit-error"></div>'
            f'<div class="buttons"><button type="button" class="button danger" id="edit-forget"'
            f' data-confirm="{esc(t["forget_confirm"])}">{esc(t["forget"])}</button>'
            f'<button type="button" class="button danger" id="edit-block" data-block="{esc(t["block"])}"'
            f' data-unblock="{esc(t["unblock"])}" data-confirm="{esc(t["block_confirm"])}">{esc(t["block"])}</button>'
            f'<span class="spacer"></span>'
            f'<button type="button" class="button" id="edit-cancel">{esc(t["cancel"])}</button>'
            f'<button type="submit" class="button filled">{esc(t["save"])}</button></div></form></dialog>')


# The views of the page, each a tab in the header: (id, label)
TABS = (("overview", "overview"), ("upstream", "upstream"), ("clients", "clients"))


def tab_of(query: dict[str, list[str]]) -> str:
    tab = (query.get("tab") or [""])[0]
    return tab if tab in dict(TABS) else "overview"


def tabs(t: dict, active: str) -> str:
    links = "".join(f'<a class="{"active" if tab == active else ""}" href="{"?" if tab == "overview" else f"?tab={tab}"}"'
                    f'{' aria-current="page"' if tab == active else ""}>{icon(tab)}<span>{esc(t[label])}</span></a>'
                    for tab, label in TABS)
    return f'<nav class="tabs">{links}</nav>'


def render(state: State, lang: str, slug: str | None, now: float | None = None, version: str | None = None,
           tab: str = "overview") -> str:
    """One view: the overview sums up, Upstream and Clients hold the details."""
    f = Format(lang)
    t = f.t
    now = state._clock() if now is None else now
    with state.lock:
        chrony = state.chrony
        if tab == "clients":
            body = clients_card(f, state, now)
        elif chrony is None:
            body = card(t["status"], f'<p class="muted">{esc(t["loading"])}</p>' if not state.at else
                        f'<div class="state bad">{icon("alert")}{esc(t["chrony_down"])}</div>')
        elif tab == "upstream":
            body = f'<div class="cards">{sources_card(f, chrony)}{accuracy_card(f, chrony)}</div>'
        else:
            body = f'<div class="cards">{clock_card(f)}{status_card(f, chrony, state.clients, now, state.http)}</div>'
    # The same header on every page of the apps: when the data were read (the
    # time of the request before the first read), refresh, and the settings,
    # which are Home Assistant's own page, outside the ingress frame
    stamp = time.strftime("%H:%M:%S", time.localtime(state.at or now))
    actions = f'<span class="stamp opt">{esc(t["as_of"].format(stamp))}</span>'
    actions += (f'<a class="icon-button" href="" id="refresh" role="button" title="{esc(t["refresh"])}"'
                f' aria-label="{esc(t["refresh"])}">{icon("refresh")}</a>')
    actions += (f'<a class="icon-button" href="/config/app/{esc(slug)}/config" target="_top"'
                f' title="{esc(t["settings"])}" aria-label="{esc(t["settings"])}">{icon("settings")}</a>'
                if slug else "")
    # As on Home Assistant's own pages: name and version at the very bottom
    about = f"{APP_NAME} {version}" if version else APP_NAME
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(t['title'])}</title><style>{stylesheet()}</style><script>{SCRIPT}</script></head><body>"
            f'<header>{APP_ICON_TAG}<h1>{esc(t["title"])}</h1>{tabs(t, tab)}<div class="actions" id="actions">{actions}</div></header>'
            # The dialog stays outside main, which the script swaps for a fresh copy
            f'<main>{body}</main>{edit_dialog(t) if tab == "clients" else ""}<footer class="version">{esc(about)}</footer></body></html>')


# --- The server ------------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Peers that may connect; any other connection is closed before a thread
    # starts for it. Empty lets everybody in (the tests).
    allowed: frozenset[str] = frozenset()

    def verify_request(self, request, client_address) -> bool:
        return not self.allowed or client_address[0] in self.allowed

    def server_bind(self) -> None:
        # HTTPServer looks up its own name here, which can wait for DNS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "chrony-web", self.server_address[1]


def handler(state: State, slug: str | None, allowed: set[str], version: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "chrony-web"
        # A connection that sends nothing frees its thread after this many seconds
        timeout = 10

        def _refused(self) -> bool:
            if allowed and self.client_address[0] not in allowed:
                self._send(403, "text/plain", b"only through Home Assistant")
            elif self.path.split("?", 1)[0] not in ("/", "/icon.png", "/now"):
                self._send(404, "text/plain", b"not found")
            else:
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
            if self._refused():
                return
            if self.path.split("?", 1)[0] == "/now":
                self._send(200, "application/json", json.dumps(state.now()).encode())
                return
            if self.path.split("?", 1)[0] == "/icon.png":
                if APP_ICON is None:
                    self._send(404, "text/plain", b"not found")
                else:
                    self._send(200, "image/png", APP_ICON, "max-age=86400")
                return
            # The refresh button asks for a read of chrony at once
            if "refresh=1" in self.path.partition("?")[2].split("&"):
                state.refresh()
            page = render(state, lang_of(self.headers.get("Cookie"), self.headers.get("Accept-Language")), slug,
                          version=version, tab=tab_of(parse_qs(self.path.partition("?")[2])))
            self._send(200, "text/html; charset=utf-8", page.encode())

        def do_POST(self) -> None:  # noqa: N802
            if self._refused():
                return
            if self.path.split("?", 1)[0] != "/":
                self._send(405, "text/plain", b"not allowed")
                return
            # Only the page's script sends JSON; a form of another site cannot
            if self.headers.get_content_type() != "application/json":
                self._send(415, "text/plain", b"JSON expected")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= BODY_MAX:
                    raise ValueError("no or too large a body")
                code = state.change(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError, AttributeError) as err:
                self._send(400, "text/plain", str(err).encode())
                return
            self._send(code, "text/plain", b"")

        def _send(self, code: int, kind: str, body: bytes, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # no access log
            pass

    return Handler


def log_level(name: str | None) -> int:
    """The app's option log_level as a level of Python's logging."""
    return {"debug": logging.DEBUG, "warning": logging.WARNING, "error": logging.ERROR}.get(
        (name or "").lower(), logging.INFO)


def main() -> None:
    logging.basicConfig(level=log_level(os.environ.get("LOG_LEVEL")),
                        format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    # The app's slug, e.g. a1b2c3d4_time_server; the Supervisor names the container's host after it
    slug = os.environ.get("HOSTNAME", "").replace("-", "_") or None
    # The version from config.yaml, passed by the Supervisor when it builds the image
    version = os.environ.get("APP_VERSION") or None
    state = State(Clients(STORE), status=STATUS, http_log=HTTP_LOG, blocked=BLOCKED, allowed=ALLOWED)
    threading.Thread(target=state.run, daemon=True).start()
    # s6 stops the service with TERM; leave through finally to save the store
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), handler(state, slug, {INGRESS_PEER}, version))
    server.allowed = frozenset({INGRESS_PEER})
    LOGGER.info("Web page on port %d (ingress)", PORT)
    try:
        server.serve_forever()
    finally:
        # The background read may be changing the clients right now
        with state.lock:
            state.clients.save(time.time())


if __name__ == "__main__":
    main()
