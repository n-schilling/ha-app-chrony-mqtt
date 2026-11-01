"""Tests for the web page. Run inside the built image:
  docker run --rm -v "$PWD/tests:/tests:ro" <image> python3 -m unittest discover -s /tests -p 'test_*.py'
chronyc is the stub answering from tests/fixtures.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = ["/usr/local/lib", str(TESTS.parent / "chrony_ntp/rootfs/usr/local/lib")]
os.environ["PATH"] = f"{TESTS / 'stubs'}:{os.environ['PATH']}"
os.environ.setdefault("TRACKING", str(TESTS / "fixtures/tracking.csv"))

import chrony_web as w  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400


def names(table: dict[str, str] | None = None) -> w.Names:
    return w.Names(resolve=lambda ip: (table or {}).get(ip, ""))


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "clients.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def state(self, read=w.read_chrony, now: float = NOW, table=None) -> w.State:
        state = w.State(w.Clients(self.path), names(table), read=read, clock=lambda: now)
        state.refresh()
        return state


class ReadChrony(Fixture):
    def test_fields(self) -> None:
        chrony = w.read_chrony()
        tr = chrony["tracking"]
        self.assertEqual((tr["refname"], tr["stratum"], tr["leap"]), ("192.53.103.108", 2, "Normal"))
        self.assertAlmostEqual(tr["offset"], 0.000091137)
        self.assertEqual((chrony["requests"], chrony["dropped"]), (62, 0))
        self.assertEqual([(s["state"], s["reach"], s["poll"], s["last_rx"]) for s in chrony["sources"]],
                         [("*", 255, 10, 756), ("?", 0, 6, None)])

    def test_clients_without_localhost(self) -> None:
        clients = w.read_chrony()["clients"]
        self.assertEqual([c["ip"] for c in clients], ["192.0.2.6", "192.0.2.7", "192.0.2.8"])
        self.assertEqual([c["last"] for c in clients], [120, 5000, None])
        self.assertEqual([c["interval"] for c in clients], [8, 6, None])

    def test_sync_problem_like_the_sensor(self) -> None:
        tracking = w.read_chrony()["tracking"]
        self.assertFalse(w.sync_problem(tracking))
        for fixture in ("tracking-unsynced.csv", "tracking-local.csv"):
            os.environ["TRACKING"] = str(TESTS / "fixtures" / fixture)
            try:
                self.assertTrue(w.sync_problem(w.read_chrony()["tracking"]), fixture)
            finally:
                os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")


class Store(Fixture):
    def test_first_and_last_seen(self) -> None:
        state = self.state(table={"192.0.2.6": "plug.lan"})
        data = state.clients.data
        # The client that only used chronyc is no NTP client
        self.assertEqual(sorted(data), ["192.0.2.6", "192.0.2.7"])
        self.assertEqual(data["192.0.2.6"], {"first_seen": NOW - 120, "last_seen": NOW - 120, "name": "plug.lan"})

    def test_kept_across_restarts(self) -> None:
        self.state()
        self.assertTrue(self.path.exists())
        gone = lambda: {**w.read_chrony(), "clients": []}  # noqa: E731
        later = self.state(read=gone, now=NOW + DAY)
        self.assertEqual(later.clients.data["192.0.2.7"]["last_seen"], NOW - 5000)

    def test_first_seen_stays(self) -> None:
        self.state()
        later = self.state(now=NOW + 600)
        entry = later.clients.data["192.0.2.6"]
        self.assertEqual((entry["first_seen"], entry["last_seen"]), (NOW - 120, NOW + 480))

    def test_forgotten_after_30_days(self) -> None:
        self.state()
        gone = lambda: {**w.read_chrony(), "clients": []}  # noqa: E731
        self.assertIn("192.0.2.6", self.state(read=gone, now=NOW + 29 * DAY).clients.data)
        self.assertEqual(self.state(read=gone, now=NOW + 31 * DAY).clients.data, {})

    def test_name_kept_when_dns_has_none(self) -> None:
        self.state(table={"192.0.2.6": "plug.lan"})
        self.assertEqual(self.state(now=NOW + 2 * w.NAME_TTL).clients.data["192.0.2.6"]["name"], "plug.lan")

    def test_broken_store_starts_empty(self) -> None:
        self.path.write_text("{")
        self.assertEqual(w.Clients(self.path).data, {})

    def test_saves_at_most_every_five_minutes(self) -> None:
        clients = w.Clients(self.path)
        clients.update([], {}, NOW)
        self.path.unlink()
        clients.update([], {}, NOW + 60)
        self.assertFalse(self.path.exists())
        clients.update([], {}, NOW + w.SAVE_EVERY)
        self.assertTrue(self.path.exists())


class Formatting(unittest.TestCase):
    def test_durations(self) -> None:
        de = w.Format("de")
        self.assertEqual([de.duration(s) for s in (0.25, 45, 125, 3600, 3720, 86400, 90000)],
                         ["0,2 s", "45 s", "2 min", "1 h", "1 h 2 min", "1 d", "1 d 1 h"])
        self.assertEqual(de.ago(300), "vor 5 min")
        self.assertEqual(w.Format("en").ago(300), "5 min ago")

    def test_numbers(self) -> None:
        self.assertEqual(w.Format("de").ms(-0.0039257), "−3,926 ms")
        self.assertEqual(w.Format("de").int(12345), "12.345")
        self.assertEqual(w.Format("en").int(12345), "12,345")
        self.assertEqual(w.Format("en").log2(10), "17 min")
        self.assertEqual(w.Format("en").log2(None), "–")

    def test_language(self) -> None:
        self.assertEqual(w.lang_of("de-DE,de;q=0.9,en;q=0.8"), "de")
        self.assertEqual(w.lang_of("en-US,de;q=0.5"), "en")
        self.assertEqual(w.lang_of("fr"), "en")
        self.assertEqual(w.lang_of(None), "en")

    def test_short_names(self) -> None:
        self.assertEqual(w.short_name("plug-kitchen.lan.example"), "plug-kitchen")
        self.assertEqual(w.short_name(""), "")


class Page(Fixture):
    def test_status_sources_and_clients(self) -> None:
        state = self.state(table={"192.0.2.6": "plug.lan"})
        page = w.render(state, "de", 3900, "abc_chrony_ntp")
        self.assertIn("Synchron mit 192.53.103.108", page)
        self.assertIn("ausgewählt", page)
        self.assertIn("nicht erreichbar", page)
        self.assertIn('data-name="plug"', page)
        self.assertIn("vor 2 min", page)
        # 192.0.2.6 asked 2 minutes ago, 192.0.2.7 83 minutes ago
        self.assertIn("Alle · 2", page)
        self.assertIn("Aktiv · 1", page)
        self.assertIn("Inaktiv · 1", page)
        self.assertIn('href="/config/app/abc_chrony_ntp/config" target="_top"', page)

    def test_upstream_names_from_dns(self) -> None:
        state = self.state(table={"192.53.103.108": "time.example.net"})
        page = w.render(state, "en", 3900, None)
        self.assertIn("Synchronized with time.example.net", page)
        self.assertIn('<td title="192.53.103.108">time.example.net', page)
        self.assertIn('<td title="192.0.2.50">192.0.2.50', page)

    def test_client_chrony_forgot(self) -> None:
        self.state()
        gone = lambda: {**w.read_chrony(), "clients": []}  # noqa: E731
        page = w.render(self.state(read=gone, now=NOW + 3600), "en", 3900, None)
        self.assertIn("1 h 2 min ago", page)
        self.assertIn('data-requests=""', page)

    def test_names_from_dns_escaped(self) -> None:
        state = self.state(table={"192.0.2.6": '<img src=x onerror="alert(1)">'})
        page = w.render(state, "en", 3900, None)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)

    def test_sync_problem(self) -> None:
        os.environ["TRACKING"] = str(TESTS / "fixtures/tracking-local.csv")
        try:
            page = w.render(self.state(), "en", 3900, None)
        finally:
            os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")
        self.assertIn('class="state bad"', page)
        self.assertIn("Serving its own clock", page)

    def test_before_the_first_read(self) -> None:
        state = w.State(w.Clients(self.path), names(), clock=lambda: NOW)
        self.assertIn("Reading chrony", w.render(state, "en", 3900, None))

    def test_chronyc_down(self) -> None:
        def fail() -> dict:
            raise subprocess.CalledProcessError(1, "chronyc")
        page = w.render(self.state(read=fail), "en", 3900, None)
        self.assertIn("chronyc does not answer.", page)
        self.assertIn("No client has asked for the time yet.", page)


class Server(Fixture):
    def serve(self, allowed: set[str]) -> str:
        server = w.Server(("127.0.0.1", 0), w.handler(self.state(), 3900, None, allowed))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, url: str, **headers) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as answer:
                return answer.status, answer.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def test_only_the_ingress_proxy(self) -> None:
        self.assertEqual(self.get(self.serve({w.INGRESS_PEER}) + "/"), (403, "only through Home Assistant"))

    def test_page_in_the_browsers_language(self) -> None:
        base = self.serve(set())
        code, page = self.get(base + "/", **{"Accept-Language": "de-DE,de;q=0.9"})
        self.assertEqual(code, 200)
        self.assertIn('<html lang="de">', page)
        self.assertEqual(self.get(base + "/other")[0], 404)

    def test_store_written_as_json(self) -> None:
        self.state()
        self.assertEqual(sorted(json.loads(self.path.read_text())["clients"]), ["192.0.2.6", "192.0.2.7"])


if __name__ == "__main__":
    unittest.main()
