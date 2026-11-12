"""Time over HTTP, for devices that set their clock from a web server instead
of NTP (htpdate, curl, small controllers).

Every answer carries chrony's time in the Date header, to the second as HTTP
defines it; a GET of / or /time also has it in the body, to the microsecond:

    {"time": 1800000000.123456, "iso": "2027-01-15T08:00:00.123456Z",
     "stratum": 3, "leap": "normal", "root_delay": 0.012, "root_dispersion": 0.001}

The time is the host clock corrected by chrony's estimate of its offset (with
set_system_clock off, chrony leaves the host clock alone and only tracks it),
read from chronyc over its local UDP port every POLL seconds; the service runs
as a user of its own, without access to chronyd's command socket. It answers
as chrony answers NTP: while chrony is not synchronized, and has no local
fallback, it sends 503 and no Date header (RFC 9110: no clock, no Date).

Only the networks chrony serves may ask (ALLOWED, written by init-chrony from
the option allowed_networks), and no address blocked on the web page
(BLOCKED, read every POLL seconds), each address at most once a second on average
in bursts of BURST, like chrony's rate limit; more is refused with 429 and
counted as dropped. What each client asked is written to CLIENTS for the web
page, which keeps it across restarts.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import math
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger("time_http")

PORT = 8080
RUN_DIR = Path("/run/time-http")
ALLOWED = RUN_DIR / "allowed"
BLOCKED = Path("/run/chrony-web/blocked.json")  # written by the web page
CLIENTS = RUN_DIR / "clients.json"
POLL = 2                 # seconds between two reads of chronyc tracking
STALE = 10 * POLL        # an older read of chrony is no reading: 503
WRITE_EVERY = 5          # seconds between two writes of the client log at most
MAX_CLIENTS = 1000       # addresses kept at most; the least recent go first
MAX_CONNECTIONS = 32     # connections served at a time; more are closed at once
RATE = 1.0               # requests per second and address on average ...
BURST = 16               # ... in bursts of up to this many (chrony: ratelimit interval 1 burst 16)
MIN_GAP = 2.0            # requests closer than this belong to one burst, not to the interval
LOCALHOST = ("127.0.0.1", "::1")
LOCAL_CLOCK = "7F7F0101"  # chrony's own clock (serve_when_offline)
PATHS = ("/", "/time")


# --- chrony ------------------------------------------------------------------------
def read_tracking() -> dict:
    """chronyc tracking over its UDP port, which answers monitoring commands to
    localhost without the command socket."""
    out = subprocess.run(["chronyc", "-h", "127.0.0.1", "-n", "-c", "tracking"], capture_output=True,
                         text=True, timeout=5, check=True)
    t = out.stdout.strip().split(",")
    return {"refid": t[0], "stratum": int(t[2]), "correction": float(t[4]), "root_delay": float(t[10]),
            "root_dispersion": float(t[11]), "leap": t[13]}


def serving(tracking: dict) -> bool:
    """Whether chrony hands out its time: synchronized, or its local fallback."""
    if tracking["refid"] == LOCAL_CLOCK:
        return True
    return 0 < tracking["stratum"] < 16 and tracking["leap"] != "Not synchronised"


class Clock:
    """chrony's time: the host clock plus chrony's latest correction."""

    def __init__(self, read=read_tracking, clock=time.time, monotonic=time.monotonic) -> None:
        self._read = read
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.tracking: dict | None = None
        self._at = -math.inf
        self._failing = False

    def refresh(self) -> None:
        try:
            tracking = self._read()
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as err:
            if not self._failing:
                self._failing = True
                LOGGER.warning("chronyc does not answer (%s); no time over HTTP until it does", err)
            return
        if self._failing:
            self._failing = False
            LOGGER.info("chronyc answers again")
        with self._lock:
            self.tracking, self._at = tracking, self._monotonic()

    def now(self) -> tuple[float, dict] | None:
        """chrony's time and its tracking, None while chrony hands out no time."""
        with self._lock:
            tracking, at = self.tracking, self._at
        if tracking is None or self._monotonic() - at > STALE or not serving(tracking):
            return None
        # chronyc's correction: positive while the host clock is slow
        return self._clock() + tracking["correction"], tracking

    def run(self, blocked: Blocked | None = None) -> None:
        while True:
            self.refresh()
            if blocked is not None:
                blocked.refresh()
            time.sleep(POLL)


# --- Who may ask, and how often --------------------------------------------------------
def read_allowed(path: Path) -> list:
    """The networks of chrony's allow lines; nothing readable lets nobody in."""
    try:
        lines = path.read_text().split()
    except OSError as err:
        LOGGER.warning("Cannot read %s (%s); answering nobody", path, err)
        return []
    networks = []
    for line in lines:
        try:
            networks.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            LOGGER.warning("Ignoring %r in %s", line, path)
    return networks


def admitted(ip: str, networks: list) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # An IPv4 client on an IPv6 socket
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address.version == n.version and address in n for n in networks)


class Blocked:
    """The addresses blocked on the web page; their connections are closed
    unanswered, as chronyd ignores their NTP requests."""

    def __init__(self, path: Path = BLOCKED) -> None:
        self._path = path
        self.addresses: frozenset = frozenset()
        self._failing = False

    def refresh(self) -> None:
        try:
            listed = json.loads(self._path.read_text())
            addresses = frozenset(ipaddress.ip_address(ip) for ip in listed)
        except FileNotFoundError:
            # Not written yet: the page writes it once chronyd has the list
            return
        except (OSError, ValueError, TypeError) as err:
            if not self._failing:
                self._failing = True
                LOGGER.warning("Cannot read %s (%s); keeping the blocked addresses known", self._path, err)
            return
        self._failing = False
        self.addresses = addresses

    def __contains__(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address in self.addresses


class Log:
    """Requests and drops per client and in all, its last request and interval,
    with a token bucket per address for the rate limit."""

    def __init__(self, clock=time.time, monotonic=time.monotonic) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.clients: dict[str, dict] = {}
        self._buckets: dict[str, tuple[float, float]] = {}
        self.requests = 0
        self.dropped = 0
        self.changed = True

    def take(self, ip: str) -> bool:
        """Counts a request; False when it is over the rate limit."""
        now, mono = self._clock(), self._monotonic()
        with self._lock:
            tokens, at = self._buckets.get(ip, (BURST, mono))
            tokens = min(BURST, tokens + (mono - at) * RATE)
            allowed = tokens >= 1
            self._buckets[ip] = (tokens - 1 if allowed else tokens, mono)
            entry = self.clients.get(ip)
            if entry is None:
                entry = self.clients[ip] = {"requests": 0, "dropped": 0, "last": now}
            elif now - entry["last"] >= MIN_GAP:
                # chrony's way: the interval as a power of two
                entry["interval"] = round(math.log2(now - entry["last"]))
            if allowed:
                entry["requests"] += 1
                self.requests += 1
            else:
                entry["dropped"] += 1
                self.dropped += 1
            entry["last"] = now
            self.changed = True
            self._limit()
            return allowed

    def _limit(self) -> None:
        excess = len(self.clients) - MAX_CLIENTS
        if excess > 0:
            for ip in sorted(self.clients, key=lambda k: self.clients[k]["last"])[:excess]:
                del self.clients[ip]
                self._buckets.pop(ip, None)

    def snapshot(self, run: str | None) -> dict:
        with self._lock:
            self.changed = False
            return {"run": run, "requests": self.requests, "dropped": self.dropped,
                    "clients": {ip: dict(e) for ip, e in self.clients.items()}}


def service_run() -> str | None:
    """Which run of this service: boot, process and its start time. A new one
    starts its counters at zero."""
    try:
        start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError):
        return None
    return f"{boot}:{os.getpid()}:{start}"


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, path)
    except OSError as err:
        LOGGER.warning("Cannot write %s: %s", path, err)


def writer(log: Log, run: str | None, path: Path = CLIENTS) -> None:
    while True:
        if log.changed:
            write_json(path, log.snapshot(run))
        time.sleep(WRITE_EVERY)


# --- The server ------------------------------------------------------------------------------
def http_date(epoch: float) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(epoch))


def body(epoch: float, tracking: dict) -> bytes:
    whole = math.floor(epoch)
    micro = min(round((epoch - whole) * 1e6), 999_999)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{micro:06d}Z"
    leap = {"Normal": "normal", "Insert second": "insert", "Delete second": "delete"}.get(tracking["leap"], "normal")
    return json.dumps({"time": round(epoch, 6), "iso": iso, "stratum": tracking["stratum"], "leap": leap,
                       "root_delay": tracking["root_delay"], "root_dispersion": tracking["root_dispersion"]}).encode()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, address, handler_class, networks: list, slots: int = MAX_CONNECTIONS,
                 blocked: Blocked | None = None) -> None:
        self.networks = networks
        self.blocked = blocked if blocked is not None else Blocked()
        self._slots = threading.BoundedSemaphore(slots)
        super().__init__(address, handler_class)

    def server_bind(self) -> None:
        # HTTPServer looks up its own name here, which can wait for DNS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "time-server", self.server_address[1]

    def verify_request(self, request, client_address) -> bool:
        # Closed before a thread starts: networks chrony does not serve,
        # blocked addresses, and connections beyond the limit
        if not admitted(client_address[0], self.networks) or client_address[0] in self.blocked:
            return False
        return self._slots.acquire(blocking=False)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def handler(clock: Clock, log: Log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "time-server"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        # A connection that sends nothing frees its slot after this many seconds
        timeout = 5

        def do_HEAD(self) -> None:  # noqa: N802 (the standard library's name)
            self._answer(with_body=False)

        def do_GET(self) -> None:  # noqa: N802
            self._answer(with_body=True)

        def _answer(self, with_body: bool) -> None:
            path = self.path.split("?", 1)[0]
            if path not in PATHS:
                self._send(404, b"not found\n", "text/plain", None, with_body)
                return
            ip = self.client_address[0]
            counted = ip not in LOCALHOST
            if counted and not log.take(ip):
                self._send(429, b"too many requests\n", "text/plain", None, with_body, {"Retry-After": "1"})
                return
            reading = clock.now()
            if reading is None:
                self._send(503, b"not synchronized\n", "text/plain", None, with_body, {"Retry-After": "60"})
                return
            epoch, tracking = reading
            self._send(200, body(epoch, tracking), "application/json", epoch, with_body)

        def _send(self, code: int, content: bytes, kind: str, epoch: float | None, with_body: bool,
                  extra: dict | None = None) -> None:
            # Without our own Date: the standard library would add the host's
            self.send_response_only(code)
            self.send_header("Server", self.server_version)
            if epoch is not None:
                self.send_header("Date", http_date(epoch))
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if with_body:
                self.wfile.write(content)

        def send_error(self, code, message=None, explain=None) -> None:
            # Malformed requests: a short answer without the host's Date
            self.close_connection = True
            with contextlib.suppress(OSError):
                self._send(code, b"bad request\n", "text/plain", None, self.command != "HEAD")

        def log_message(self, *args) -> None:  # no access log
            pass

    return Handler


def log_level(name: str | None) -> int:
    return {"debug": logging.DEBUG, "warning": logging.WARNING, "error": logging.ERROR}.get(
        (name or "").lower(), logging.INFO)


def main() -> None:
    logging.basicConfig(level=log_level(os.environ.get("LOG_LEVEL")),
                        format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    clock, log, blocked = Clock(), Log(), Blocked()
    networks = read_allowed(ALLOWED)
    blocked.refresh()
    threading.Thread(target=clock.run, args=(blocked,), daemon=True).start()
    threading.Thread(target=writer, args=(log, service_run()), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), handler(clock, log), networks, blocked=blocked)
    LOGGER.info("Time over HTTP on port %d", PORT)
    try:
        server.serve_forever()
    finally:
        write_json(CLIENTS, log.snapshot(service_run()))


if __name__ == "__main__":
    main()
