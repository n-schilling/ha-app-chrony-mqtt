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
        self.assertEqual({k: data["192.0.2.6"][k] for k in ("first_seen", "last_seen", "name", "requests")},
                         {"first_seen": NOW - 120, "last_seen": NOW - 120, "name": "plug.lan", "requests": 12})

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


class Totals(Fixture):
    @staticmethod
    def counting(requests: int, dropped: int = 0, run: str | None = None, clients: dict | None = None):
        """chrony's figures with its counters set; clients maps an address to (requests, dropped)."""
        def read() -> dict:
            chrony = {**w.read_chrony(), "requests": requests, "dropped": dropped, "run": run}
            if clients is not None:
                chrony["clients"] = [{"ip": ip, "requests": r, "dropped": d, "interval": 10, "last": 60}
                                     for ip, (r, d) in clients.items()]
            return chrony
        return read

    def requests(self, state: w.State, ip: str) -> tuple[int, int]:
        entry = state.clients.data[ip]
        return w.Clients.sum(entry, "requests"), w.Clients.sum(entry, "dropped")

    def test_client_counts_survive_a_new_chronyd(self) -> None:
        both = {"192.0.2.6": (30, 1), "192.0.2.7": (8, 0)}
        self.state(read=self.counting(38, 1, "a", both))
        # The new chronyd has heard from one client so far, and that one asked more often already
        later = self.state(read=self.counting(40, 0, "b", {"192.0.2.6": (40, 0)}), now=NOW + 600)
        self.assertEqual(self.requests(later, "192.0.2.6"), (70, 1))
        self.assertEqual(self.requests(later, "192.0.2.7"), (8, 0))
        self.assertEqual(later.clients.total("requests"), 78)
        # The second client asks again: counted from zero on top of the 8
        again = self.state(read=self.counting(45, 0, "b", {"192.0.2.6": (42, 0), "192.0.2.7": (3, 0)}),
                           now=NOW + 1200)
        self.assertEqual(self.requests(again, "192.0.2.7"), (11, 0))
        self.assertEqual(self.requests(again, "192.0.2.6"), (72, 1))

    def test_same_chronyd_counts_only_the_growth(self) -> None:
        self.state(read=self.counting(10, 0, "a", {"192.0.2.6": (10, 0)}))
        later = self.state(read=self.counting(25, 0, "a", {"192.0.2.6": (25, 0)}), now=NOW + 600)
        self.assertEqual((self.requests(later, "192.0.2.6"), later.clients.total("requests")), ((25, 0), 25))

    def test_client_dropped_from_chronys_log(self) -> None:
        self.state(read=self.counting(50, 0, "a", {"192.0.2.6": (50, 0)}))
        # Same chronyd, but the client's counter started again
        later = self.state(read=self.counting(52, 0, "a", {"192.0.2.6": (2, 0)}), now=NOW + 600)
        self.assertEqual(self.requests(later, "192.0.2.6"), (52, 0))

    def test_store_of_1_5(self) -> None:
        # No run and no client counters yet: the counters are taken as they are
        self.path.write_text(json.dumps({
            "clients": {"192.0.2.6": {"first_seen": NOW - 600, "last_seen": NOW - 60, "name": ""}},
            "totals": {"since": NOW - 600, "base_requests": 0, "base_dropped": 0, "requests": 30, "dropped": 0}}))
        state = self.state(read=self.counting(35, 0, "a", {"192.0.2.6": (20, 0)}))
        self.assertEqual((state.clients.total("requests"), self.requests(state, "192.0.2.6")), (35, (20, 0)))
        self.assertEqual(state.clients.totals["run"], "a")

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs Linux's /proc (runs in the image)")
    def test_chronyd_run(self) -> None:
        pidfile = Path(self.tmp.name) / "chronyd.pid"
        pidfile.write_text(f"{os.getpid()}\n")
        real = w.PIDFILE
        w.PIDFILE = pidfile
        try:
            run = w.chrony_run()
            self.assertEqual(run, w.chrony_run())
            self.assertEqual(run.split(":")[1], str(os.getpid()))
            w.PIDFILE = Path(self.tmp.name) / "missing.pid"
            self.assertIsNone(w.chrony_run())
        finally:
            w.PIDFILE = real

    def test_totals_survive_a_restart(self) -> None:
        self.state(read=self.counting(100, 3))
        self.state(read=self.counting(150, 4), now=NOW + 600)
        # The app restarted: chrony counts from zero again
        later = self.state(read=self.counting(20, 1), now=NOW + 1200)
        self.assertEqual((later.clients.total("requests"), later.clients.total("dropped")), (170, 5))
        self.assertEqual(later.clients.totals["since"], NOW)
        stored = json.loads(self.path.read_text())["totals"]
        self.assertEqual((stored["base_requests"], stored["requests"]), (150, 20))

    def test_growing_counters_are_not_added_twice(self) -> None:
        clients = w.Clients(self.path)
        for n in (10, 20, 30):
            clients.count({"requests": n, "dropped": 0, "run": "a"}, NOW)
        self.assertEqual(clients.total("requests"), 30)

    def test_store_of_1_4_without_totals(self) -> None:
        self.path.write_text(json.dumps({"clients": {}}))
        state = self.state(read=self.counting(7))
        self.assertEqual(state.clients.total("requests"), 7)

    def test_page_shows_totals_and_the_counters_since_the_start(self) -> None:
        self.state(read=self.counting(1200, 2))
        page = w.render(self.state(read=self.counting(62), now=NOW + 60), "de", None, now=NOW + 60)
        self.assertIn("1.262", page)
        self.assertIn("62 seit App-Start", page)
        self.assertIn("seit " + w.Format("de").day(NOW), page)


    def test_new_client_with_a_new_chronyd_counted_once(self) -> None:
        self.state(read=self.counting(10, 0, "a", {"192.0.2.6": (10, 0)}))
        later = self.state(read=self.counting(5, 0, "b", {"192.0.2.6": (2, 0), "192.0.2.9": (3, 0)}), now=NOW + 600)
        self.assertEqual(self.requests(later, "192.0.2.9"), (3, 0))
        self.assertEqual(self.requests(later, "192.0.2.6"), (12, 0))


def asking(clients: dict[str, tuple[int | None, int, int]], run: str = "a"):
    """chrony's figures with clients given as address: (interval, seconds since the last request, requests)."""
    def read() -> dict:
        return {**w.read_chrony(), "run": run,
                "clients": [{"ip": ip, "requests": r, "dropped": 0, "interval": i, "last": last}
                            for ip, (i, last, r) in clients.items()]}
    return read


class Activity(Fixture):
    def test_active_by_the_clients_own_interval(self) -> None:
        # Every 68 minutes: active 70 minutes after its last request, idle after four hours
        state = self.state(read=asking({"192.0.2.6": (12, 4200, 5)}))
        self.assertTrue(w.Clients.active(state.clients.data["192.0.2.6"], NOW))
        self.assertFalse(w.Clients.active(state.clients.data["192.0.2.6"], NOW - 4200 + 3 * 4096 + 3600))
        # Every 4 minutes: idle after 1 h 13 min
        state = self.state(read=asking({"192.0.2.7": (8, 4500, 5)}))
        self.assertFalse(w.Clients.active(state.clients.data["192.0.2.7"], NOW))

    def test_interval_kept_across_restarts(self) -> None:
        self.state(read=asking({"192.0.2.6": (16, 60, 5)}))
        gone = asking({}, run="b")
        later = self.state(read=gone, now=NOW + 2 * DAY)
        self.assertEqual(later.clients.data["192.0.2.6"]["interval"], 16)
        self.assertTrue(w.Clients.active(later.clients.data["192.0.2.6"], NOW + 2 * DAY))

    def test_unknown_interval_counts_as_chronys_default(self) -> None:
        state = self.state(read=asking({"192.0.2.6": (None, 60, 1)}))
        entry = state.clients.data["192.0.2.6"]
        self.assertNotIn("interval", entry)
        self.assertTrue(w.Clients.active(entry, NOW - 60 + 3 * 1024 + 3599))
        self.assertFalse(w.Clients.active(entry, NOW - 60 + 3 * 1024 + 3600))

    def test_requests_missing_only_when_no_client_is_active(self) -> None:
        state = self.state(read=asking({"192.0.2.6": (8, 60, 5), "192.0.2.7": (12, 40000, 5)}))
        self.assertFalse(state.clients.requests_missing(NOW))
        self.assertTrue(state.clients.requests_missing(NOW + 2 * 3600))
        self.assertFalse(w.Clients(Path(self.tmp.name) / "none.json").requests_missing(NOW))

    def test_status_for_the_publisher(self) -> None:
        status = Path(self.tmp.name) / "status.json"
        state = w.State(w.Clients(self.path), names(), read=asking({"192.0.2.6": (8, 60, 5)}),
                        clock=lambda: NOW, status=status)
        state.refresh()
        self.assertEqual(json.loads(status.read_text()), {"at": NOW, "requests_missing": False})
        # chronyc does not answer for hours: the kept clients tell
        def fail() -> dict:
            raise subprocess.CalledProcessError(1, "chronyc")
        later = w.State(w.Clients(self.path), names(), read=fail, clock=lambda: NOW + 3 * 3600, status=status)
        later.refresh()
        self.assertTrue(json.loads(status.read_text())["requests_missing"])

    def test_page_tells_when_requests_are_missing(self) -> None:
        self.state(read=asking({"192.0.2.6": (8, 60, 5)}))
        page = w.render(self.state(read=asking({}, run="b"), now=NOW + 3 * 3600), "de", None)
        self.assertIn("Kein Client fragt mehr an, letzte Anfrage vor 3 h 1 min", page)
        self.assertNotIn("Kein Client fragt", w.render(self.state(read=asking({"192.0.2.6": (8, 60, 6)})), "de", None))

    def test_note_explains_activity_and_counting(self) -> None:
        page = w.render(self.state(), "de", None)
        self.assertIn("weniger als drei Intervalle des Clients und 1 h zurück", page)
        self.assertIn("frühestens seit " + w.Format("de").day(NOW), page)


class Editing(Fixture):
    def test_name_and_note(self) -> None:
        state = self.state(table={"192.0.2.6": "plug.lan"})
        self.assertEqual(state.change({"action": "edit", "ip": "192.0.2.6", "name": " Fridge ", "note": "behind\nthe door"}), 204)
        entry = json.loads(self.path.read_text())["clients"]["192.0.2.6"]
        self.assertEqual((entry["alias"], entry["note"], entry["name"]), ("Fridge", "behind the door", "plug.lan"))
        page = w.render(state, "en", None)
        self.assertIn('data-name="fridge"', page)
        self.assertIn('<span class="sub memo" title="behind the door">behind the door</span>', page)
        self.assertIn('data-text="fridge plug.lan 192.0.2.6 behind the door"', page)
        self.assertIn('data-dns="plug"', page)
        # Empty fields drop name and note again
        state.change({"action": "edit", "ip": "192.0.2.6", "name": "", "note": ""})
        self.assertNotIn("alias", state.clients.data["192.0.2.6"])
        self.assertNotIn("note", state.clients.data["192.0.2.6"])

    def test_plain_text_only(self) -> None:
        state = self.state()
        state.change({"action": "edit", "ip": "192.0.2.6", "name": "a\x00b‮c", "note": "<b>x</b>"})
        entry = state.clients.data["192.0.2.6"]
        self.assertEqual(entry["alias"], "a b c")
        self.assertNotIn("<b>", w.render(state, "en", None))
        self.assertEqual(w.clean("x" * 120, w.NOTE_MAX), "x" * 120)
        for value in ("x" * 121, 5, None):
            with self.assertRaises(ValueError):
                state.change({"action": "edit", "ip": "192.0.2.6", "name": "", "note": value})

    def test_unknown_client_or_action(self) -> None:
        state = self.state()
        self.assertEqual(state.change({"action": "edit", "ip": "192.0.2.99", "name": "x", "note": ""}), 404)
        for body in ({"action": "drop", "ip": "192.0.2.6"}, {"action": "edit"}):
            with self.assertRaises(ValueError):
                state.change(body)

    def test_named_client_stays_until_forgotten(self) -> None:
        state = self.state()
        state.change({"action": "edit", "ip": "192.0.2.6", "name": "", "note": "spare plug"})
        gone = asking({}, run="b")
        later = self.state(read=gone, now=NOW + 40 * DAY)
        self.assertEqual(sorted(later.clients.data), ["192.0.2.6"])


class Forgetting(Fixture):
    def requests(self, state: w.State, ip: str) -> int:
        return w.Clients.sum(state.clients.data[ip], "requests")

    def test_forgotten_until_it_asks_again(self) -> None:
        state = self.state(read=asking({"192.0.2.6": (8, 60, 30)}))
        self.assertEqual(state.change({"action": "forget", "ip": "192.0.2.6"}), 204)
        self.assertNotIn("192.0.2.6", json.loads(self.path.read_text())["clients"])
        # chrony still knows it, but nothing new
        state._read = asking({"192.0.2.6": (8, 75, 30)})
        state.refresh()
        self.assertNotIn("192.0.2.6", state.clients.data)
        # A new request: back, counted from then on
        state._read = asking({"192.0.2.6": (8, 5, 32)})
        state.refresh()
        self.assertEqual(self.requests(state, "192.0.2.6"), 2)
        self.assertEqual(state.clients.forgotten, {})

    def test_new_chronyd_forgets_too(self) -> None:
        state = self.state(read=asking({"192.0.2.6": (8, 60, 30)}))
        state.change({"action": "forget", "ip": "192.0.2.6"})
        later = self.state(read=asking({"192.0.2.6": (8, 5, 1)}, run="b"), now=NOW + 600)
        self.assertEqual(self.requests(later, "192.0.2.6"), 1)

    def test_client_chrony_does_not_know(self) -> None:
        self.state(read=asking({"192.0.2.6": (8, 60, 30)}))
        state = self.state(read=asking({}, run="b"), now=NOW + 600)
        state.change({"action": "forget", "ip": "192.0.2.6"})
        self.assertEqual((state.clients.data, state.clients.forgotten), ({}, {}))
        self.assertEqual(state.change({"action": "forget", "ip": "192.0.2.6"}), 404)


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
        page = w.render(state, "de", "abc_chrony_ntp")
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
        page = w.render(state, "en", None)
        self.assertIn("Synchronized with time.example.net", page)
        self.assertIn('<td title="192.53.103.108">time.example.net', page)
        self.assertIn('<td title="192.0.2.50">192.0.2.50', page)

    def test_client_chrony_forgot(self) -> None:
        self.state()
        gone = lambda: {**w.read_chrony(), "clients": []}  # noqa: E731
        page = w.render(self.state(read=gone, now=NOW + 3600), "en", None)
        self.assertIn("1 h 2 min ago", page)
        # Its requests and the last interval chrony knew stay from the store
        self.assertIn('data-requests="12" data-dropped="0" data-interval="8"', page)

    def test_names_from_dns_escaped(self) -> None:
        state = self.state(table={"192.0.2.6": '<img src=x onerror="alert(1)">'})
        page = w.render(state, "en", None)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)

    def test_sync_problem(self) -> None:
        os.environ["TRACKING"] = str(TESTS / "fixtures/tracking-local.csv")
        try:
            page = w.render(self.state(), "en", None)
        finally:
            os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")
        self.assertIn('class="state bad"', page)
        self.assertIn("Serving its own clock", page)

    def test_before_the_first_read(self) -> None:
        state = w.State(w.Clients(self.path), names(), clock=lambda: NOW)
        self.assertIn("Reading chrony", w.render(state, "en", None))

    def test_chronyc_down(self) -> None:
        def fail() -> dict:
            raise subprocess.CalledProcessError(1, "chronyc")
        page = w.render(self.state(read=fail), "en", None)
        self.assertIn("chronyc does not answer.", page)
        self.assertIn("No client has asked for the time yet.", page)


class Server(Fixture):
    def serve(self, allowed: set[str]) -> str:
        server = w.Server(("127.0.0.1", 0), w.handler(self.state(), None, allowed))
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

    def test_refresh_button_reads_chrony_at_once(self) -> None:
        reads = []
        state = self.state()
        real = state._read
        state._read = lambda: reads.append(1) or real()
        server = w.Server(("127.0.0.1", 0), w.handler(state, None, set()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        code, page = self.get(base + "/")
        self.assertEqual((code, len(reads)), (200, 0))
        self.assertIn('id="refresh"', page)
        self.assertEqual(self.get(base + "/?refresh=1")[0], 200)
        self.assertEqual(len(reads), 1)

    def post(self, url: str, body: bytes, kind: str = "application/json") -> int:
        request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": kind})
        try:
            with urllib.request.urlopen(request) as answer:
                return answer.status
        except urllib.error.HTTPError as err:
            with err:
                return err.code

    def test_changes_from_the_page(self) -> None:
        base = self.serve(set()) + "/"
        edit = json.dumps({"action": "edit", "ip": "192.0.2.6", "name": "Fridge", "note": "kitchen"}).encode()
        self.assertEqual(self.post(base, edit), 204)
        self.assertEqual(json.loads(self.path.read_text())["clients"]["192.0.2.6"]["alias"], "Fridge")
        # A form of another site cannot send JSON
        self.assertEqual(self.post(base, b"action=forget&ip=192.0.2.6", "application/x-www-form-urlencoded"), 415)
        self.assertEqual(self.post(base, b"{", "application/json"), 400)
        self.assertEqual(self.post(base, json.dumps({"action": "edit", "ip": "192.0.2.6", "name": "",
                                                     "note": "x" * 121}).encode()), 400)
        self.assertEqual(self.post(base, b"x" * (w.BODY_MAX + 1)), 400)
        self.assertEqual(self.post(base, json.dumps({"action": "forget", "ip": "192.0.2.99"}).encode()), 404)
        self.assertEqual(self.post(base, json.dumps({"action": "forget", "ip": "192.0.2.6"}).encode()), 204)

    def test_changes_only_through_the_ingress_proxy(self) -> None:
        base = self.serve({w.INGRESS_PEER}) + "/"
        self.assertEqual(self.post(base, json.dumps({"action": "forget", "ip": "192.0.2.6"}).encode()), 403)

    def test_page_has_the_dialog_outside_main(self) -> None:
        page = self.get(self.serve(set()) + "/")[1]
        self.assertIn('</main><dialog id="edit"', page)
        self.assertIn(f'maxlength="{w.NOTE_MAX}"', page)
        self.assertIn('class="edit"', page)

    def test_store_written_as_json(self) -> None:
        self.state()
        self.assertEqual(sorted(json.loads(self.path.read_text())["clients"]), ["192.0.2.6", "192.0.2.7"])


if __name__ == "__main__":
    unittest.main()
