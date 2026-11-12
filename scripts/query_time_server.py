#!/usr/bin/env python3
"""Asks a Time Server from outside, over NTP and over HTTP, and compares both
with this computer's clock. Python's standard library only.

    scripts/query_time_server.py HOST [--ntp-port 123] [--http-port 80] [--count 4]

NTP: an SNTP request (RFC 4330); offset and round trip as NTP computes them
from the four timestamps. HTTP: a GET of /time; the server's time against the
middle of the round trip, error half the round trip, and the Date header.
Each is asked --count times, the answer with the shortest round trip counts.
Exit status 0 when both answered with a synchronized time, 1 otherwise; a
protocol left out with --no-ntp or --no-http does not count.

The offsets say how far this computer's clock is off: positive means this
computer is ahead of the server.
"""

from __future__ import annotations

import argparse
import email.utils
import http.client
import json
import socket
import struct
import sys
import time

NTP_EPOCH = 2208988800  # seconds from 1900 to 1970
LEAP = {0: "normal", 1: "insert second", 2: "delete second", 3: "not synchronized"}


def ntp_time(raw: int) -> float:
    return raw / 2**32 - NTP_EPOCH


def ask_ntp(host: str, port: int, timeout: float) -> dict:
    """One SNTP request: version 4, mode 3 (client), our send time as transmit timestamp."""
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        raw_t1 = int((t1 + NTP_EPOCH) * 2**32)
        sock.sendto(struct.pack("!B39xQ", (0 << 6) | (4 << 3) | 3, raw_t1), (host, port))
        data, _ = sock.recvfrom(512)
        t4 = time.time()
    if len(data) < 48:
        raise ValueError("short answer")
    first, stratum, _poll, precision, root_delay, root_disp, refid, _ref, origin, rx, tx = struct.unpack(
        "!BBbbII4sQQQQ", data[:48])
    if origin != raw_t1:
        raise ValueError("answer to another request")
    if stratum == 0:
        # A kiss-o'-death packet: the server refuses, e.g. RATE
        raise ValueError(f"refused ({refid.decode(errors='replace')})")
    t2, t3 = ntp_time(rx), ntp_time(tx)
    leap = first >> 6
    return {"server": t3, "offset": ((t1 - t2) + (t4 - t3)) / 2, "delay": (t4 - t1) - (t3 - t2),
            "stratum": stratum, "leap": LEAP[leap], "synced": leap != 3 and stratum < 16,
            "refid": refid.decode() if stratum == 1 else socket.inet_ntoa(refid),
            "precision": 2.0**precision, "root_delay": root_delay / 2**16, "root_dispersion": root_disp / 2**16}


def ask_http(host: str, port: int, timeout: float) -> dict:
    """A GET of /time; the server's time is taken for the middle of the round trip."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        # Connect first, so the round trip is one request and its answer
        conn.connect()
        t1 = time.time()
        conn.request("GET", "/time", headers={"Accept": "application/json"})
        answer = conn.getresponse()
        body = answer.read()
        t4 = time.time()
    finally:
        conn.close()
    date = answer.getheader("Date")
    result = {"status": answer.status, "delay": t4 - t1, "date": date, "synced": False}
    if date:
        result["date_offset"] = (t1 + t4) / 2 - email.utils.parsedate_to_datetime(date).timestamp()
    if answer.status == 200:
        data = json.loads(body)
        result.update(server=data["time"], offset=(t1 + t4) / 2 - data["time"], stratum=data["stratum"],
                      leap=data["leap"], root_delay=data["root_delay"], root_dispersion=data["root_dispersion"],
                      synced=True)
    return result


def best(ask, count: int) -> tuple[dict | None, list[str]]:
    """The answer with the shortest round trip, and the errors of the others."""
    answers, errors = [], []
    for n in range(count):
        try:
            answers.append(ask())
        except (OSError, ValueError, KeyError, http.client.HTTPException) as err:
            errors.append(str(err) or type(err).__name__)
        if n + 1 < count:
            # Within the server's rate limit: one a second on average
            time.sleep(1.1)
    return (min(answers, key=lambda a: a["delay"]) if answers else None), errors


def ms(seconds: float) -> str:
    return f"{seconds * 1000:+.3f} ms"


def show_time(epoch: float) -> str:
    whole = int(epoch // 1)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(whole)) + f".{int((epoch - whole) * 1e6):06d} UTC"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host", help="the server's address or name, e.g. homeassistant.local")
    parser.add_argument("--ntp-port", type=int, default=123)
    parser.add_argument("--http-port", type=int, default=80,
                        help="the host port set for 8080/tcp in the app's network settings (default 80)")
    parser.add_argument("--count", type=int, default=4, help="requests per protocol (default 4)")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for an answer (default 3)")
    parser.add_argument("--no-ntp", action="store_true", help="ask over HTTP only")
    parser.add_argument("--no-http", action="store_true", help="ask over NTP only")
    args = parser.parse_args()
    ok = True

    if not args.no_ntp:
        print(f"NTP   {args.host}:{args.ntp_port}")
        answer, errors = best(lambda: ask_ntp(args.host, args.ntp_port, args.timeout), args.count)
        if answer is None:
            print(f"      no answer: {'; '.join(errors)}")
            ok = False
        else:
            print(f"      server time  {show_time(answer['server'])}")
            print(f"      offset       {ms(answer['offset'])}   (this computer against the server)")
            print(f"      round trip   {ms(answer['delay'])}")
            print(f"      stratum {answer['stratum']}, reference {answer['refid']}, leap {answer['leap']}, "
                  f"root delay {ms(answer['root_delay'])}, root dispersion {ms(answer['root_dispersion'])}")
            ok &= answer["synced"]
            if errors:
                print(f"      {len(errors)} of {args.count} failed: {'; '.join(errors)}")

    if not args.no_http:
        print(f"HTTP  http://{args.host}:{args.http_port}/time")
        answer, errors = best(lambda: ask_http(args.host, args.http_port, args.timeout), args.count)
        if answer is None:
            print(f"      no answer: {'; '.join(errors)}")
            ok = False
        elif answer["status"] != 200:
            print(f"      answered {answer['status']}{' (not synchronized)' if answer['status'] == 503 else ''}")
            ok = False
        else:
            print(f"      server time  {show_time(answer['server'])}")
            print(f"      offset       {ms(answer['offset'])}   ± {abs(answer['delay']) * 500:.3f} ms "
                  "(half the round trip)")
            print(f"      round trip   {ms(answer['delay'])}")
            print(f"      Date header  {answer['date']}   (offset {answer['date_offset']:+.3f} s, to the second)")
            print(f"      stratum {answer['stratum']}, leap {answer['leap']}, "
                  f"root delay {ms(answer['root_delay'])}, root dispersion {ms(answer['root_dispersion'])}")
            if errors:
                print(f"      {len(errors)} of {args.count} failed: {'; '.join(errors)}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
