"""Tests for the HTTP time service. Run inside the built image, with the other
Python tests:
  docker run --rm -v "$PWD/tests:/tests:ro" <image> python3 -m unittest discover -s /tests -p 'test_*.py'
"""

import email.utils
import http.client
import ipaddress
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = ["/usr/local/lib", str(TESTS.parent / "time_server/rootfs/usr/local/lib")]

import time_http as h  # noqa: E402

NOW = 1_800_000_000.25
SYNCED = {"refid": "C035676C", "stratum": 2, "correction": 0.5, "root_delay": 0.012, "root_dispersion": 0.002,
          "leap": "Normal"}


class Ticks:
    """A clock the tests move by hand."""

    def __init__(self, at: float = 0.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def clock(tracking: dict | None = SYNCED, now: float = NOW) -> h.Clock:
    c = h.Clock(read=lambda: dict(tracking), clock=lambda: now, monotonic=Ticks())
    if tracking is not None:
        c.refresh()
    return c


class Time(unittest.TestCase):
    def test_host_clock_plus_chronys_correction(self) -> None:
        epoch, tracking = clock().now()
        self.assertEqual(epoch, NOW + 0.5)
        self.assertEqual(tracking["stratum"], 2)

    def test_no_time_while_unsynchronized(self) -> None:
        for change in ({"leap": "Not synchronised"}, {"stratum": 16}, {"stratum": 0}):
            self.assertIsNone(clock({**SYNCED, **change}).now(), change)

    def test_local_fallback_hands_out_time(self) -> None:
        self.assertIsNotNone(clock({**SYNCED, "refid": h.LOCAL_CLOCK, "stratum": 10}).now())

    def test_no_time_from_a_stale_read(self) -> None:
        ticks = Ticks()
        c = h.Clock(read=lambda: dict(SYNCED), clock=lambda: NOW, monotonic=ticks)
        c.refresh()
        ticks.at = h.STALE + 1
        self.assertIsNone(c.now())

    def test_no_time_before_the_first_read(self) -> None:
        self.assertIsNone(clock(None).now())

    def test_chronyc_failure_logged_once(self) -> None:
        def fail() -> dict:
            raise OSError("no chronyc")
        c = h.Clock(read=fail)
        with self.assertLogs("time_http", "WARNING") as logs:
            c.refresh()
            c.refresh()
        self.assertEqual(len(logs.output), 1)

    def test_body(self) -> None:
        data = json.loads(h.body(NOW, SYNCED))
        self.assertEqual(data, {"time": NOW, "iso": "2027-01-15T08:00:00.250000Z", "stratum": 2, "leap": "normal",
                                "root_delay": 0.012, "root_dispersion": 0.002})

    def test_http_date(self) -> None:
        self.assertEqual(h.http_date(NOW), "Fri, 15 Jan 2027 08:00:00 GMT")


class Networks(unittest.TestCase):
    def test_allowed_like_chrony(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowed"
            path.write_text("10.0.0.0/8\nfd00::/8\nnonsense\n")
            with self.assertLogs("time_http", "WARNING"):
                networks = h.read_allowed(path)
        self.assertEqual(networks, [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8")])
        self.assertTrue(h.admitted("10.1.2.3", networks))
        self.assertTrue(h.admitted("::ffff:10.1.2.3", networks))
        self.assertTrue(h.admitted("fd00::1", networks))
        self.assertFalse(h.admitted("192.0.2.1", networks))
        self.assertFalse(h.admitted("not an address", networks))

    def test_nothing_readable_lets_nobody_in(self) -> None:
        with self.assertLogs("time_http", "WARNING"):
            self.assertEqual(h.read_allowed(Path("/nonexistent/allowed")), [])


class Log(unittest.TestCase):
    def test_rate_limit_like_chrony(self) -> None:
        mono = Ticks()
        log = h.Log(clock=lambda: NOW, monotonic=mono)
        self.assertEqual([log.take("192.0.2.1") for _ in range(h.BURST + 2)], [True] * h.BURST + [False, False])
        # Another address has a burst of its own
        self.assertTrue(log.take("192.0.2.2"))
        # One more a second
        mono.at = 1
        self.assertEqual([log.take("192.0.2.1"), log.take("192.0.2.1")], [True, False])
        snap = log.snapshot("run")
        self.assertEqual(snap["clients"]["192.0.2.1"], {"requests": h.BURST + 1, "dropped": 3, "last": NOW})
        self.assertEqual((snap["requests"], snap["dropped"], snap["run"]), (h.BURST + 2, 3, "run"))

    def test_interval_as_a_power_of_two(self) -> None:
        wall = Ticks(NOW)
        log = h.Log(clock=wall, monotonic=Ticks())
        log.take("192.0.2.1")
        wall.at += 1  # a burst, no interval
        log.take("192.0.2.1")
        self.assertNotIn("interval", log.clients["192.0.2.1"])
        wall.at += 1000
        log.take("192.0.2.1")
        self.assertEqual(log.clients["192.0.2.1"]["interval"], 10)

    def test_bounded(self) -> None:
        wall = Ticks(NOW)
        log = h.Log(clock=wall, monotonic=Ticks())
        for n in range(h.MAX_CLIENTS + 5):
            wall.at += 1
            log.take(f"10.0.{n // 256}.{n % 256}")
        self.assertEqual(len(log.clients), h.MAX_CLIENTS)
        self.assertNotIn("10.0.0.0", log.clients)

    def test_written_only_when_changed(self) -> None:
        log = h.Log()
        log.snapshot(None)
        self.assertFalse(log.changed)
        log.take("192.0.2.1")
        self.assertTrue(log.changed)


class Server(unittest.TestCase):
    def serve(self, c: h.Clock, networks: str = "127.0.0.0/8", slots: int = h.MAX_CONNECTIONS) -> tuple[int, h.Log]:
        log = h.Log()
        server = h.Server(("127.0.0.1", 0), h.handler(c, log), [ipaddress.ip_network(networks)], slots)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], log

    def ask(self, port: int, method: str = "GET", path: str = "/") -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path)
        answer = conn.getresponse()
        answer.body = answer.read()
        return answer

    def test_time_in_the_date_header_and_the_body(self) -> None:
        port, _ = self.serve(clock())
        for path in ("/", "/time"):
            answer = self.ask(port, path=path)
            self.assertEqual(answer.status, 200)
            self.assertEqual(email.utils.parsedate_to_datetime(answer.getheader("Date")).timestamp(), 1_800_000_000)
            self.assertEqual(json.loads(answer.body)["time"], NOW + 0.5)
            self.assertEqual(answer.getheader("Content-Type"), "application/json")
            self.assertEqual(answer.getheader("Cache-Control"), "no-store")
            self.assertEqual(answer.getheader("Server"), "time-server")

    def test_head_like_htpdate(self) -> None:
        port, _ = self.serve(clock())
        answer = self.ask(port, "HEAD")
        self.assertEqual((answer.status, answer.body), (200, b""))
        self.assertEqual(answer.getheader("Date"), "Fri, 15 Jan 2027 08:00:00 GMT")

    def test_no_date_without_time(self) -> None:
        port, _ = self.serve(clock({**SYNCED, "leap": "Not synchronised"}))
        answer = self.ask(port)
        self.assertEqual(answer.status, 503)
        self.assertIsNone(answer.getheader("Date"))
        self.assertEqual(answer.getheader("Retry-After"), "60")

    def test_other_paths_and_methods(self) -> None:
        port, _ = self.serve(clock())
        answer = self.ask(port, path="/admin")
        self.assertEqual(answer.status, 404)
        self.assertIsNone(answer.getheader("Date"))
        self.assertEqual(self.ask(port, "POST").status, 501)

    def test_localhost_not_counted(self) -> None:
        port, log = self.serve(clock())
        self.ask(port)
        self.assertEqual(log.clients, {})

    def test_other_networks_closed_at_once(self) -> None:
        port, _ = self.serve(clock(), networks="192.0.2.0/24")
        with self.assertRaises((ConnectionError, http.client.HTTPException)):
            self.ask(port)

    def test_connections_beyond_the_limit_closed(self) -> None:
        port, _ = self.serve(clock(), slots=1)
        held = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(held.close)
        held.request("GET", "/")
        self.assertEqual(held.getresponse().read()[:1], b"{")  # kept alive: holds the one slot
        with self.assertRaises((ConnectionError, http.client.HTTPException)):
            self.ask(port)

    def test_idle_connections_time_out(self) -> None:
        self.assertEqual(h.handler(clock(), h.Log()).timeout, 5)


if __name__ == "__main__":
    unittest.main()
