"""Tests for the web page. Run inside the built image:
  docker run --rm -v "$PWD/tests:/tests:ro" <image> python3 -m unittest discover -s /tests -p 'test_*.py'
chronyc is the stub answering from tests/fixtures.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = ["/usr/local/lib", str(TESTS.parent / "time_server/rootfs/usr/local/lib")]
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

    def test_broken_store_is_kept_aside(self) -> None:
        self.path.write_text("{")
        with self.assertLogs("chrony_web", "WARNING") as log:
            clients = w.Clients(self.path)
        self.assertEqual(clients.data, {})
        self.assertIn("clients.json.broken", log.output[0])
        self.assertEqual(self.path.with_name("clients.json.broken").read_text(), "{")
        self.assertFalse(self.path.exists())

    def test_store_of_the_wrong_type_is_kept_aside(self) -> None:
        self.path.write_text("[]")
        with self.assertLogs("chrony_web", "WARNING"):
            self.assertEqual(w.Clients(self.path).data, {})
        self.assertTrue(self.path.with_name("clients.json.broken").exists())

    def test_no_store_yet_is_no_warning(self) -> None:
        with self.assertNoLogs("chrony_web", "WARNING"):
            self.assertEqual(w.Clients(self.path).data, {})

    def test_save_leaves_no_temporary_file(self) -> None:
        clients = w.Clients(self.path)
        clients.save(NOW)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["clients.json"])

    def test_save_that_fails_keeps_the_old_store(self) -> None:
        clients = w.Clients(self.path)
        clients.data["192.0.2.6"] = {"first_seen": 1, "last_seen": 1, "name": ""}
        clients.save(NOW)
        clients.data["192.0.2.7"] = {"bad": object()}
        with self.assertLogs("chrony_web", "WARNING"):
            clients.save(NOW + 1)
        self.assertEqual(sorted(json.loads(self.path.read_text())["clients"]), ["192.0.2.6"])
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["clients.json"])

    def test_at_most_max_clients(self) -> None:
        clients = w.Clients(self.path)
        clients.data["192.0.2.1"] = {"first_seen": 0, "last_seen": 0, "name": "", "alias": "Old clock"}
        clients.update([{"ip": "192.0.2.2", "requests": 50, "dropped": 0, "interval": 10, "last": 9000}], {}, NOW)
        flood = [{"ip": f"2001:db8::{n:x}", "requests": 1, "dropped": 0, "interval": None, "last": 1}
                 for n in range(w.MAX_CLIENTS + 500)]
        with self.assertLogs("chrony_web", "WARNING") as log:
            clients.update(flood, {}, NOW + 60)
        self.assertEqual(len(clients.data), w.MAX_CLIENTS)
        # Named and regular clients stay; addresses that asked once go first
        self.assertIn("192.0.2.1", clients.data)
        self.assertIn("192.0.2.2", clients.data)
        self.assertIn("More than 1000 clients", log.output[0])
        # One warning per hour
        with self.assertNoLogs("chrony_web", "WARNING"):
            clients.update(flood[:10] + [{"ip": "2001:db8:1::1", "requests": 1, "dropped": 0, "interval": None,
                                          "last": 1}], {}, NOW + 120)

    def test_saves_at_most_every_five_minutes(self) -> None:
        clients = w.Clients(self.path)
        clients.update([], {}, NOW)
        self.path.unlink()
        clients.update([], {}, NOW + 60)
        self.assertFalse(self.path.exists())
        clients.update([], {}, NOW + w.SAVE_EVERY)
        self.assertTrue(self.path.exists())


class Names(unittest.TestCase):
    def test_lookups_in_the_background(self) -> None:
        release = threading.Event()
        asked = []

        def slow(ip: str) -> str:
            asked.append(ip)
            release.wait(5)
            return "clock.lan"

        found = w.Names()
        found._resolve = slow
        # Answers at once, the name comes with a later read
        self.assertEqual(found.get("192.0.2.6", NOW), "")
        self.assertEqual(found.get("192.0.2.6", NOW), "")
        release.set()
        for _ in range(50):
            if found.get("192.0.2.6", time.time()):
                break
            threading.Event().wait(0.05)
        self.assertEqual(found.get("192.0.2.6", time.time()), "clock.lan")
        self.assertEqual(asked, ["192.0.2.6"])

    def test_waiting_lookups_are_bounded(self) -> None:
        found = w.Names()
        block = threading.Event()
        found._resolve = lambda ip: block.wait(5) and ""
        for n in range(w.LOOKUPS_MAX + 100):
            found.get(f"2001:db8::{n:x}", NOW)
        self.assertEqual(len(found._waiting), w.LOOKUPS_MAX)
        block.set()

    def test_answers_kept_are_bounded(self) -> None:
        found = names()
        for n in range(w.NAMES_MAX + 10):
            found.get(f"2001:db8::{n:x}", NOW + n)
        self.assertEqual(len(found._known), w.NAMES_MAX)
        self.assertNotIn("2001:db8::0", found._known)


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

    def test_requests_missing_counts_the_clients_of_the_last_week(self) -> None:
        state = self.state(read=asking({"192.0.2.6": (8, 60, 5)}))
        state.clients.edit("192.0.2.6", "Taken away", "", NOW)
        # A named device taken away holds the sensor on for a week, not for ever
        self.assertTrue(state.clients.requests_missing(NOW - 60 + w.RECENT - 1))
        self.assertFalse(state.clients.requests_missing(NOW - 60 + w.RECENT))
        self.assertIn("192.0.2.6", state.clients.data)
        # One client of the last week that stopped asking is enough
        state.clients.data["192.0.2.7"] = {"last_seen": NOW - 60 + w.RECENT - 6 * 3600, "interval": 8}
        self.assertTrue(state.clients.requests_missing(NOW - 60 + w.RECENT))

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
        page = w.render(self.state(), "de", None, tab="clients")
        self.assertIn("weniger als drei Intervalle des Clients und 1 h zurück", page)
        self.assertIn("frühestens seit " + w.Format("de").day(NOW), page)


class Views(Fixture):
    def test_tabs_in_the_header(self) -> None:
        state = self.state()
        for tab in ("overview", "upstream", "clients"):
            page = w.render(state, "en", None, tab=tab)
            header = page[page.index("<header>"):page.index("</header>")]
            self.assertRegex(header, r'^<header><img class="app-icon"[^>]*><h1>Time Server</h1><nav class="tabs">')
            self.assertEqual(re.findall(r'<a class="[a-z]*" href="([^"]*)"', header), ["?", "?tab=upstream", "?tab=clients"])
            self.assertEqual(re.findall(r'<a class="active" href="([^"]*)" aria-current="page">', header),
                             ["?" if tab == "overview" else f"?tab={tab}"])
        self.assertIn("<span>Übersicht</span>", w.render(state, "de", None))

    def test_each_detail_once(self) -> None:
        state = self.state()
        overview, upstream, clients = (w.render(state, "en", None, tab=t) for t in ("overview", "upstream", "clients"))
        self.assertIn('href="?tab=upstream"><svg', overview)
        self.assertIn('href="?tab=clients"><svg', overview)
        self.assertIn("RMS offset", upstream)
        self.assertNotIn("RMS offset", overview + clients)
        self.assertIn('id="clients"', clients)
        self.assertNotIn('id="clients"', overview + upstream)
        self.assertIn("System offset", overview)
        self.assertNotIn("System offset", upstream + clients)

    def test_tab_from_the_url(self) -> None:
        base = Server.serve(self, set())
        self.assertIn('id="clients"', Server.get(self, base + "/?tab=clients")[1])
        self.assertIn('id="clients"', Server.get(self, base + "/?tab=clients&refresh=1")[1])
        self.assertIn("Synchronized with", Server.get(self, base + "/?tab=nothing")[1])


class Editing(Fixture):
    def test_name_and_note(self) -> None:
        state = self.state(table={"192.0.2.6": "plug.lan"})
        self.assertEqual(state.change({"action": "edit", "ip": "192.0.2.6", "name": " Fridge ", "note": "behind\nthe door"}), 204)
        entry = json.loads(self.path.read_text())["clients"]["192.0.2.6"]
        self.assertEqual((entry["alias"], entry["note"], entry["name"]), ("Fridge", "behind the door", "plug.lan"))
        page = w.render(state, "en", None, tab="clients")
        self.assertIn('data-name="fridge"', page)
        self.assertIn('<span class="sub memo" title="behind the door">behind the door</span>', page)
        self.assertIn('data-text="fridge plug.lan 192.0.2.6 behind the door ntp"', page)
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


class Http(Fixture):
    """Clients of the HTTP time service, taken from its log."""

    def log(self, clients: dict, run: str = "h1", requests: int | None = None, dropped: int = 0) -> None:
        total = sum(c[0] for c in clients.values()) if requests is None else requests
        self.http.write_text(json.dumps({"run": run, "requests": total, "dropped": dropped, "clients": {
            ip: {"requests": r, "dropped": d, "last": last, **({"interval": i} if i is not None else {})}
            for ip, (r, d, last, i) in clients.items()}}))

    def http_state(self, read=None, now: float = NOW) -> w.State:
        state = w.State(w.Clients(self.path), names(), read=read or asking({}), clock=lambda: now,
                        http_log=self.http)
        state.refresh()
        return state

    def setUp(self) -> None:
        super().setUp()
        self.http = Path(self.tmp.name) / "http.json"

    def test_no_log_yet(self) -> None:
        state = self.http_state()
        self.assertEqual(state.clients.data, {})
        self.assertNotIn("http", state.clients.totals)
        self.assertNotIn("HTTP requests", w.render(state, "en", None))

    def test_http_client_listed_with_its_protocol(self) -> None:
        self.log({"192.0.2.20": (3, 1, NOW - 50, 6), "127.0.0.1": (9, 0, NOW, None)})
        state = self.http_state()
        entry = state.clients.data["192.0.2.20"]
        self.assertEqual(sorted(state.clients.data), ["192.0.2.20"])
        self.assertEqual((w.Clients.protocols(entry), w.Clients.requests(entry), w.Clients.dropped(entry)),
                         (["HTTP"], 3, 1))
        self.assertEqual((entry["last_seen"], w.interval_of(entry)), (NOW - 50, 6))
        page = w.render(state, "en", None, tab="clients")
        self.assertIn('<td class="proto">HTTP</td>', page)
        self.assertIn('data-requests="3" data-dropped="1" data-protocol="http" data-interval="6"', page)
        self.assertIn('<th data-sort="protocol">Protocol</th>', page)

    def test_client_over_both_protocols(self) -> None:
        self.log({"192.0.2.6": (5, 0, NOW - 10, None)})
        state = self.http_state(read=asking({"192.0.2.6": (8, 60, 30)}))
        entry = state.clients.data["192.0.2.6"]
        self.assertEqual((w.Clients.protocols(entry), w.Clients.requests(entry)), (["NTP", "HTTP"], 35))
        # The later request of the two
        self.assertEqual(entry["last_seen"], NOW - 10)
        # chrony's interval comes first
        self.assertEqual(w.interval_of(entry), 8)
        self.assertIn('<td class="proto">NTP, HTTP</td>', w.render(state, "en", None, tab="clients"))

    def test_totals_on_the_overview(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, None)}, requests=7, dropped=2)
        page = w.render(self.http_state(), "de", None)
        self.assertRegex(page, r'<th>HTTP-Anfragen</th><td>7<span class="sub">seit [0-9.]+ · 7 seit App-Start · '
                               r'Verworfen 2</span>')

    def test_counts_survive_a_new_service(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, None)})
        self.http_state()
        self.log({"192.0.2.20": (1, 0, NOW + 10, None)}, run="h2")
        state = self.http_state(now=NOW + 20)
        self.assertEqual(w.Clients.requests(state.clients.data["192.0.2.20"]), 4)
        self.assertEqual(w.Clients.sum(state.clients.totals["http"], "requests"), 4)

    def test_same_service_counts_only_the_growth(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, None)})
        state = self.http_state()
        self.log({"192.0.2.20": (5, 0, NOW - 5, None)})
        state.refresh()
        self.assertEqual(w.Clients.requests(state.clients.data["192.0.2.20"]), 5)

    def test_forgotten_until_it_asks_again(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, None)})
        state = self.http_state()
        self.assertEqual(state.change({"action": "forget", "ip": "192.0.2.20"}), 204)
        state.refresh()
        self.assertNotIn("192.0.2.20", state.clients.data)
        self.log({"192.0.2.20": (4, 0, NOW - 1, None)})
        state.refresh()
        self.assertEqual(w.Clients.requests(state.clients.data["192.0.2.20"]), 1)
        self.assertEqual(state.clients.forgotten_http, {})

    def test_forgotten_over_ntp_comes_back_over_http(self) -> None:
        state = self.http_state(read=asking({"192.0.2.6": (8, 60, 30)}))
        state.change({"action": "forget", "ip": "192.0.2.6"})
        self.log({"192.0.2.6": (2, 0, NOW - 1, None)})
        state.refresh()
        entry = state.clients.data["192.0.2.6"]
        # Over HTTP only: chrony's counters from before are not taken in
        self.assertEqual((w.Clients.protocols(entry), w.Clients.requests(entry)), (["HTTP"], 2))
        state._read = asking({"192.0.2.6": (8, 5, 31)})
        state.refresh()
        self.assertEqual(w.Clients.requests(state.clients.data["192.0.2.6"]), 3)

    def test_kept_in_the_store(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, 6)})
        self.http_state().clients.save(NOW)
        self.http.unlink()
        state = self.http_state(now=NOW + 60)
        self.assertEqual(w.Clients.requests(state.clients.data["192.0.2.20"]), 3)

    def test_http_client_counts_for_requests_missing(self) -> None:
        self.log({"192.0.2.20": (3, 0, NOW - 50, 6)})
        self.assertFalse(self.http_state().clients.requests_missing(NOW))
        self.assertTrue(self.http_state(now=NOW + DAY).clients.requests_missing(NOW + DAY))

    def test_broken_log_logged_once(self) -> None:
        self.http.write_text("{")
        with self.assertLogs("chrony_web", "WARNING") as logs:
            state = self.http_state()
            state.refresh()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(state.clients.data, {})


class Clock(Fixture):
    def test_chronys_time(self) -> None:
        state = self.state()
        self.assertEqual(state.now(), {"time": NOW + 0.000091137, "stratum": 2})

    def test_no_time_while_unsynchronized(self) -> None:
        os.environ["TRACKING"] = str(TESTS / "fixtures/tracking-unsynced.csv")
        try:
            self.assertEqual(self.state().now(), {"time": None})
        finally:
            os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")

    def test_local_fallback_hands_out_time(self) -> None:
        os.environ["TRACKING"] = str(TESTS / "fixtures/tracking-local.csv")
        try:
            self.assertIsNotNone(self.state().now()["time"])
        finally:
            os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")

    def test_before_the_first_read(self) -> None:
        state = w.State(w.Clients(self.path), names(), clock=lambda: NOW)
        self.assertEqual(state.now(), {"time": None})

    def test_card_on_the_overview_only(self) -> None:
        state = self.state()
        self.assertIn('<div class="clock" id="clock"', w.render(state, "de", None))
        self.assertIn("<h2>Aktuelle Zeit</h2>", w.render(state, "de", None))
        self.assertNotIn('id="clock"', w.render(state, "de", None, tab="clients"))
        self.assertNotIn('id="clock"', w.render(state, "de", None, tab="upstream"))


class Blocking(Fixture):
    """Clients blocked on the page: chronyd denies them, the HTTP time service reads the list."""

    def setUp(self) -> None:
        super().setUp()
        self.commands: list[tuple[str, ...]] = []
        self.blocked = Path(self.tmp.name) / "blocked.json"
        self.allowed = Path(self.tmp.name) / "allowed"
        self.allowed.write_text("192.0.2.0/24\n")

    def blocking_state(self, read=None, now: float = NOW, fail: bool = False) -> w.State:
        def command(*args: str) -> None:
            if fail:
                raise subprocess.CalledProcessError(1, "chronyc")
            self.commands.append(args)
        state = w.State(w.Clients(self.path), names(), read=read or asking({"192.0.2.6": (8, 60, 30)}),
                        clock=lambda: now, blocked=self.blocked, allowed=self.allowed, command=command)
        state.refresh()
        return state

    def test_block_and_unblock(self) -> None:
        state = self.blocking_state()
        self.assertEqual(state.change({"action": "block", "ip": "192.0.2.6"}), 204)
        self.assertEqual(self.commands, [("deny", "192.0.2.6")])
        self.assertEqual(json.loads(self.blocked.read_text()), ["192.0.2.6"])
        self.assertTrue(json.loads(self.path.read_text())["clients"]["192.0.2.6"]["blocked"])
        # Blocking twice changes nothing
        self.assertEqual(state.change({"action": "block", "ip": "192.0.2.6"}), 204)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(state.change({"action": "unblock", "ip": "192.0.2.6"}), 204)
        self.assertEqual(self.commands[-1], ("allow", "192.0.2.6"))
        self.assertEqual(json.loads(self.blocked.read_text()), [])
        self.assertNotIn("blocked", state.clients.data["192.0.2.6"])

    def test_unblock_outside_the_served_networks_allows_nothing(self) -> None:
        self.allowed.write_text("10.0.0.0/8\n")
        state = self.blocking_state()
        state.change({"action": "block", "ip": "192.0.2.6"})
        state.change({"action": "unblock", "ip": "192.0.2.6"})
        self.assertEqual(self.commands, [("deny", "192.0.2.6")])

    def test_addresses_only(self) -> None:
        state = self.blocking_state()
        for ip in ("192.0.2.6\nallow all", "all", "192.0.2.0/24"):
            with self.assertRaises(ValueError):
                state.change({"action": "block", "ip": ip})
        self.assertEqual(state.change({"action": "block", "ip": "192.0.2.99"}), 404)
        self.assertEqual(self.commands, [])

    def test_chronyc_failure_changes_nothing(self) -> None:
        state = self.blocking_state(fail=True)
        with self.assertLogs("chrony_web", "WARNING"):
            self.assertEqual(state.change({"action": "block", "ip": "192.0.2.6"}), 503)
        self.assertNotIn("blocked", state.clients.data["192.0.2.6"])

    def test_given_to_every_new_chronyd(self) -> None:
        state = self.blocking_state()
        state.change({"action": "block", "ip": "192.0.2.6"})
        self.commands.clear()
        state.refresh()
        self.assertEqual(self.commands, [])
        state._read = asking({"192.0.2.6": (8, 60, 30)}, run="b")
        state.refresh()
        self.assertEqual(self.commands, [("deny", "192.0.2.6")])

    def test_given_after_a_restart_of_the_app(self) -> None:
        self.blocking_state().change({"action": "block", "ip": "192.0.2.6"})
        self.commands.clear()
        self.blocking_state()
        self.assertEqual(self.commands, [("deny", "192.0.2.6")])
        self.assertEqual(json.loads(self.blocked.read_text()), ["192.0.2.6"])

    def test_stays_listed_until_unblocked(self) -> None:
        state = self.blocking_state()
        state.change({"action": "block", "ip": "192.0.2.6"})
        self.assertEqual(state.change({"action": "forget", "ip": "192.0.2.6"}), 409)
        later = self.blocking_state(read=asking({}, run="b"), now=NOW + 60 * DAY)
        self.assertIn("192.0.2.6", later.clients.data)

    def test_not_counted_for_requests_missing(self) -> None:
        state = self.blocking_state(read=asking({"192.0.2.6": (8, 60, 30), "192.0.2.7": (8, 60, 30)}))
        state.change({"action": "block", "ip": "192.0.2.6"})
        self.assertFalse(state.clients.requests_missing(NOW))
        state.change({"action": "block", "ip": "192.0.2.7"})
        self.assertFalse(state.clients.requests_missing(NOW))

    def test_an_older_list_never_overwrites_a_newer_one(self) -> None:
        # A new chronyd gets the list while the page blocks a client
        state = self.blocking_state()
        started, release = threading.Event(), threading.Event()

        def slow(*args: str) -> None:
            self.commands.append(args)
            if args == ("deny", "192.0.2.6") and not started.is_set():
                started.set()
                release.wait(5)
        state.change({"action": "block", "ip": "192.0.2.6"})
        state._command = slow
        state._read = asking({"192.0.2.6": (8, 60, 30), "192.0.2.7": (8, 60, 30)}, run="b")
        renewing = threading.Thread(target=state.refresh)
        renewing.start()
        started.wait(5)
        blocking = threading.Thread(target=state.change, args=({"action": "block", "ip": "192.0.2.7"},))
        blocking.start()
        release.set()
        renewing.join(5)
        blocking.join(5)
        self.assertEqual(json.loads(self.blocked.read_text()), ["192.0.2.6", "192.0.2.7"])

    def test_shown_in_the_list(self) -> None:
        state = self.blocking_state()
        page = w.render(state, "de", None, tab="clients")
        self.assertNotIn('data-show="blocked"', page)
        state.change({"action": "block", "ip": "192.0.2.6"})
        page = w.render(state, "de", None, tab="clients")
        self.assertIn('data-state="blocked" data-blocked="1"', page)
        self.assertIn('<span class="sub blocked">Blockiert: bekommt keine Zeit</span>', page)
        self.assertIn('<button class="chip" data-show="blocked">Blockiert · 1</button>', page)
        self.assertIn('<span class="dot blocked" title="Blockiert">', page)
        self.assertIn('id="edit-block" data-block="Blockieren" data-unblock="Freigeben"', page)


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
        # Without the cookie of share/ha-app/ha-lang.js: the browser's languages
        self.assertEqual(w.lang_of(None, "de-DE,de;q=0.9,en;q=0.8"), "de")
        self.assertEqual(w.lang_of(None, "en-US,de;q=0.5"), "en")
        self.assertEqual(w.lang_of("other=1", "fr"), "en")
        self.assertEqual(w.lang_of(None, None), "en")

    def test_language_of_home_assistant(self) -> None:
        # The cookie decides, whatever the browser asks for
        self.assertEqual(w.lang_of("a=b; ha_lang=de", "en-US"), "de")
        self.assertEqual(w.lang_of("ha_lang=en", "de-DE,de"), "en")
        self.assertEqual(w.lang_of("ha_lang=fr", "de-DE,de"), "en")

    def test_shared_language_script_first(self) -> None:
        shared = w.SHARED_SCRIPT_FILE.read_text(encoding="utf-8")
        self.assertIn("ha_lang", shared)
        self.assertTrue(w.SCRIPT.startswith(shared))

    def test_short_names(self) -> None:
        self.assertEqual(w.short_name("plug-kitchen.lan.example"), "plug-kitchen")
        self.assertEqual(w.short_name(""), "")


class Page(Fixture):
    def test_status_sources_and_clients(self) -> None:
        state = self.state(table={"192.0.2.6": "plug.lan"})
        overview = w.render(state, "de", "abc_time_server")
        self.assertIn("Synchron mit 192.53.103.108", overview)
        self.assertIn("Clients: 1 aktiv, 1 inaktiv", overview)
        upstream = w.render(state, "de", "abc_time_server", tab="upstream")
        self.assertIn("ausgewählt", upstream)
        self.assertIn("nicht erreichbar", upstream)
        page = w.render(state, "de", "abc_time_server", tab="clients")
        self.assertIn('data-name="plug"', page)
        self.assertIn("vor 2 min", page)
        # 192.0.2.6 asked 2 minutes ago, 192.0.2.7 83 minutes ago
        self.assertIn("Alle · 2", page)
        self.assertIn("Aktiv · 1", page)
        self.assertIn("Inaktiv · 1", page)
        self.assertIn('href="/config/app/abc_time_server/config" target="_top"', page)

    def test_upstream_names_from_dns(self) -> None:
        state = self.state(table={"192.53.103.108": "time.example.net"})
        self.assertIn("Synchronized with time.example.net", w.render(state, "en", None))
        page = w.render(state, "en", None, tab="upstream")
        self.assertIn('<td title="192.53.103.108">time.example.net', page)
        self.assertIn('<td title="192.0.2.50">192.0.2.50', page)

    def test_client_chrony_forgot(self) -> None:
        self.state()
        gone = lambda: {**w.read_chrony(), "clients": []}  # noqa: E731
        page = w.render(self.state(read=gone, now=NOW + 3600), "en", None, tab="clients")
        self.assertIn("1 h 2 min ago", page)
        # Its requests and the last interval chrony knew stay from the store
        self.assertIn('data-requests="12" data-dropped="0" data-protocol="ntp" data-interval="8"', page)

    def test_names_from_dns_escaped(self) -> None:
        state = self.state(table={"192.0.2.6": '<img src=x onerror="alert(1)">'})
        page = w.render(state, "en", None, tab="clients")
        self.assertNotIn("<img src=x", page)
        self.assertEqual(page.count("<img"), 1)  # the app's icon in the header
        self.assertIn("&lt;img", page)

    def test_never_synchronized(self) -> None:
        os.environ["TRACKING"] = str(TESTS / "fixtures/tracking-stratum0.csv")
        try:
            page = w.render(self.state(), "de", None)
        finally:
            os.environ["TRACKING"] = str(TESTS / "fixtures/tracking.csv")
        self.assertIn("Nicht synchronisiert", page)
        self.assertNotIn("Schaltsekunden", page)

    def test_version_at_the_bottom(self) -> None:
        page = w.render(self.state(), "en", None, version="2.1.0")
        self.assertIn('<footer class="version">Time Server 2.1.0</footer>', page)
        self.assertIn('<footer class="version">Time Server</footer>', w.render(self.state(), "en", None))

    def test_same_header_as_every_app(self) -> None:
        """h1, then stamp, refresh and settings, in this order (STANDARD.md, section 7)."""
        state = self.state()
        page = w.render(state, "de", "a1b2c3d4_time_server")
        stamp = time.strftime("%H:%M:%S", time.localtime(state.at))
        self.assertRegex(page, '<header><img class="app-icon" src="icon.png" alt="" width="24" height="24"><h1>Time Server</h1>'
                               '<nav class="tabs">.*</nav><div class="actions" id="actions">'
                               f'<span class="stamp opt">Stand {stamp}</span>'
                               '<a class="icon-button" href="" id="refresh" role="button" title="Aktualisieren"[^>]*>'
                               '<svg[^>]*><path[^>]*/></svg></a>'
                               '<a class="icon-button" href="/config/app/a1b2c3d4_time_server/config" target="_top"'
                               ' title="Einstellungen"[^>]*><svg[^>]*><path[^>]*/></svg></a></div></header>')
        # Without a slug no settings button; before the first read the time of the request
        state.at = 0.0
        page = w.render(state, "en", None, now=NOW)
        self.assertIn(f'<span class="stamp opt">As of {time.strftime("%H:%M:%S", time.localtime(NOW))}</span>', page)
        self.assertNotIn("/config/app/", page)

    def test_shared_stylesheet(self) -> None:
        page = w.render(self.state(), "en", None)
        self.assertIn("footer.version", page)
        self.assertIn("--primary-color: #009ac7", page)
        self.assertIn(".dot.on", page)

    def test_log_level_from_the_option(self) -> None:
        import logging
        self.assertEqual([w.log_level(n) for n in ("debug", "info", "warning", "error", None)],
                         [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.INFO])

    def test_chronyc_failure_logged_once(self) -> None:
        calls = []

        def flaky() -> dict:
            calls.append(1)
            if len(calls) <= 2:
                raise subprocess.CalledProcessError(1, "chronyc")
            return w.read_chrony()

        state = w.State(w.Clients(self.path), names(), read=flaky, clock=lambda: NOW)
        with self.assertLogs("chrony_web", "INFO") as log:
            state.refresh()
            state.refresh()
            state.refresh()
        self.assertEqual([r.levelname for r in log.records], ["WARNING", "INFO"])
        self.assertIn("chronyc does not answer", log.output[0])

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
        state = self.state(read=fail)
        self.assertIn("chronyc does not answer.", w.render(state, "en", None))
        self.assertIn("chronyc does not answer.", w.render(state, "en", None, tab="upstream"))
        self.assertIn("No client has asked for the time yet.", w.render(state, "en", None, tab="clients"))


class Server(Fixture):
    def serve(self, allowed: set[str]) -> str:
        server = w.Server(("127.0.0.1", 0), w.handler(self.state(), None, allowed, "2.1.0"))
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

    def test_other_peers_closed_before_a_thread_starts(self) -> None:
        server = w.Server(("127.0.0.1", 0), w.handler(self.state(), None, set()))
        server.allowed = frozenset({w.INGRESS_PEER})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises((urllib.error.URLError, ConnectionError)):
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5)

    def test_time_for_the_clock(self) -> None:
        code, body = self.get(self.serve(set()) + "/now")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"time": NOW + 0.000091137, "stratum": 2})

    def test_idle_connections_time_out(self) -> None:
        self.assertEqual(w.handler(self.state(), None, set()).timeout, 10)

    def test_page_in_the_browsers_language(self) -> None:
        base = self.serve(set())
        code, page = self.get(base + "/", **{"Accept-Language": "de-DE,de;q=0.9"})
        self.assertEqual(code, 200)
        self.assertIn('<html lang="de">', page)
        self.assertEqual(self.get(base + "/other")[0], 404)

    def test_page_in_home_assistants_language(self) -> None:
        base = self.serve(set())
        page = self.get(base + "/", **{"Cookie": "ha_lang=de", "Accept-Language": "en"})[1]
        self.assertIn('<html lang="de">', page)
        page = self.get(base + "/", **{"Cookie": "ha_lang=en", "Accept-Language": "de"})[1]
        self.assertIn('<html lang="en">', page)

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

    def test_app_icon(self) -> None:
        # The store icon, copied into the image; the page links it relative to the ingress path
        # (in the image the checkout is not there; the image's copy is then the one to serve)
        repo = TESTS.parent / "time_server/icon.png"
        icon = repo.read_bytes() if repo.exists() else w.APP_ICON
        self.assertTrue(icon and icon.startswith(b"\x89PNG"), "icon.png in the image or the checkout")
        base = self.serve(set())
        w.APP_ICON, saved = icon, w.APP_ICON
        try:
            with urllib.request.urlopen(base + "/icon.png") as answer:
                self.assertEqual((answer.status, answer.headers["Content-Type"], answer.read()), (200, "image/png", icon))
                self.assertEqual(answer.headers["Cache-Control"], "max-age=86400")
            self.assertEqual(self.post(base + "/icon.png", b"{}"), 405)
            w.APP_ICON = None
            self.assertEqual(self.get(base + "/icon.png")[0], 404)
        finally:
            w.APP_ICON = saved
        self.assertIn('<header><img class="app-icon" src="icon.png" alt="" width="24" height="24"><h1>',
                      self.get(base + "/")[1])

    def test_page_has_the_dialog_outside_main(self) -> None:
        base = self.serve(set())
        self.assertNotIn("<dialog", self.get(base + "/")[1])
        page = self.get(base + "/?tab=clients")[1]
        self.assertRegex(page, r'</main><dialog id="edit".*</dialog><footer class="version">Time Server 2\.1\.0</footer></body>')
        self.assertIn(f'maxlength="{w.NOTE_MAX}"', page)
        self.assertIn('class="icon-button small edit"', page)

    def test_store_written_as_json(self) -> None:
        self.state()
        self.assertEqual(sorted(json.loads(self.path.read_text())["clients"]), ["192.0.2.6", "192.0.2.7"])


if __name__ == "__main__":
    unittest.main()
