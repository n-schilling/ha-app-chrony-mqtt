"""The Page check, and the screenshots in docs/screenshots.

Starts the built image twice in a throwaway Docker network: once as the app,
its page on made-up data (tests/page/demo.py, host name a1b2c3d4-chrony-ntp,
so the slug is a1b2c3d4_chrony_ntp), once as a stand-in for Home Assistant at
the ingress proxy's address 172.30.32.2 (tests/page/ingress.py), which passes
the page on under /api/hassio_ingress/<token>/ and holds it in a frame. A
headless browser then opens the page as Home Assistant's ingress would and
checks, in light, dark and at phone width, in English and German:

- the header (STANDARD.md, section 7): the name, then the stamp "As of
  HH:MM:SS", the refresh button and the settings button, which leads to
  /config/app/a1b2c3d4_chrony_ntp/config outside the frame (target _top);
- the version line "Chrony NTP + MQTT <version>" as the last element of body;
- no console errors, no failed scripts and no Content Security Policy violations;
- refresh, the client list's filter and the edit dialog work, and nothing is
  wider than a phone;
- the page takes Home Assistant's language over the browser's (a stand-in for
  its frontend in German, English and French around the frame): reloading once
  when it changes, never again, with the cookie for the ingress path only.

Then it takes the screenshots, unless --check is given.

    scripts/screenshots.py [--check] [--image chrony-mqtt:check] [--build]
    uv run --with playwright==1.58.0 python scripts/screenshots.py --build

--build builds the image first (with the version from config.yaml). Uses the
installed Google Chrome, else Playwright's Chromium (`playwright install
chromium` once). Every name, address and number on the pictures is made up;
the addresses are from the ranges reserved for documentation.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error, Frame, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/screenshots"
VERSION = re.search(r'^version: "?([^"\n]+)"?$', (ROOT / "chrony_ntp/config.yaml").read_text(), re.M)[1]
NAME = "Chrony NTP + MQTT"
HOST = "a1b2c3d4-chrony-ntp"
SLUG = "a1b2c3d4_chrony_ntp"
INGRESS = "/api/hassio_ingress/demo-token/"
NET, APP, PROXY = "chrony-page-check", "chrony-page-check-app", "chrony-page-check-ingress"
TEXTS = {"en-US": ("NTP server", "As of", "Refresh", "Settings"),
         "de-DE": ("NTP-Server", "Stand", "Aktualisieren", "Einstellungen")}
# Collects Content Security Policy violations, which reach no console
WATCH_CSP = """window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(e.violatedDirective + " " + e.blockedURI));"""

problems: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("    ok: " if ok else "    FAILED: ") + what)
    if not ok:
        problems.append(what)


def docker(*args: str, check_: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check_, capture_output=True, text=True).stdout.strip()


def cleanup() -> None:
    docker("rm", "-f", APP, PROXY, check_=False)
    docker("network", "rm", NET, check_=False)


def start(image: str) -> str:
    """The app and the ingress stand-in; the address of the stand-in on this machine."""
    cleanup()
    # Another app's Page check on this machine may hold the subnet for a while
    for _ in range(100):
        made = subprocess.run(["docker", "network", "create", "--subnet", "172.30.32.0/23", NET],
                              capture_output=True, text=True)
        if made.returncode == 0:
            break
        if "overlaps" not in made.stderr:
            raise SystemExit(made.stderr)
        print("    waiting for 172.30.32.0/23, in use by another network")
        time.sleep(3)
    else:
        raise SystemExit("172.30.32.0/23 stays in use by another Docker network")
    tests = f"{ROOT / 'tests'}:/tests:ro"
    docker("run", "-d", "--name", APP, "--network", NET, "--ip", "172.30.32.10", "--hostname", HOST,
           "-e", "TZ=Europe/Berlin", "-v", tests, "--entrypoint", "python3", image, "/tests/page/demo.py")
    docker("run", "-d", "--name", PROXY, "--network", NET, "--ip", "172.30.32.2", "-p", "127.0.0.1::8080",
           "-v", tests, "--entrypoint", "python3", image, "/tests/page/ingress.py", "http://172.30.32.10:8099")
    port = docker("port", PROXY, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + INGRESS, timeout=2) as answer:
                # Ready once the first read of chronyc and the name lookups are in
                if answer.status == 200 and b"purifier-bedroom.lan" in answer.read():
                    return base
        except OSError:
            pass
        time.sleep(1)
    print(docker("logs", APP, check_=False))
    raise SystemExit("The page did not come up")


def header(p: Page | Frame, lang: str, phone: bool) -> None:
    title, as_of, refresh, settings = TEXTS[lang]
    shape = p.evaluate("""() => {
      const h = document.querySelector("body > header");
      const a = h && h.querySelector(":scope > .actions");
      return h && {
        first: h.firstElementChild.outerHTML.slice(0, 4), h1: h.querySelector(":scope > h1")?.textContent,
        kids: a ? [...a.children].map((e) => ({ tag: e.tagName, id: e.id, cls: e.className, role: e.getAttribute("role"),
          href: e.getAttribute("href"), target: e.getAttribute("target"), title: e.title, text: e.textContent,
          shown: e.offsetParent !== null, icon: !!e.querySelector("svg path") })) : [],
        last: document.body.lastElementChild.outerHTML,
      };
    }""")
    check(shape is not None, f"{lang}: header")
    if shape is None:
        return
    kids = shape["kids"]
    check(shape["first"] == "<h1>" and shape["h1"] == title, f"{lang}: header starts with h1 {title!r}")
    check(len(kids) == 3, f"{lang}: three actions in the header ({len(kids)})")
    if len(kids) == 3:
        stamp, again, gear = kids
        check(stamp["tag"] == "SPAN" and "stamp" in stamp["cls"].split() and "opt" in stamp["cls"].split()
              and re.fullmatch(rf"{as_of} \d\d:\d\d:\d\d", stamp["text"]) is not None,
              f"{lang}: stamp {stamp['text']!r}")
        check(stamp["shown"] != phone, f"{lang}: stamp {'hidden' if phone else 'shown'} at this width")
        check(again["tag"] == "A" and again["id"] == "refresh" and again["href"] == "" and again["role"] == "button"
              and again["title"] == refresh and again["icon"], f"{lang}: refresh button")
        check(gear["tag"] == "A" and gear["href"] == f"/config/app/{SLUG}/config" and gear["target"] == "_top"
              and gear["title"] == settings and gear["icon"], f"{lang}: settings button ({gear['href']})")
    check(shape["last"] == f'<footer class="version">{NAME} {VERSION}</footer>', f"{lang}: version line last")


def run(image: str, shots: bool) -> None:
    base = start(image)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Error:
            browser = pw.chromium.launch()

        def page(width: int, height: int, scheme: str, lang: str, url: str = base + INGRESS) -> Page:
            context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                          device_scale_factor=2, locale=lang,
                                          extra_http_headers={"Accept-Language": lang})
            context.add_init_script(WATCH_CSP)
            p = context.new_page()
            p.errors = []
            p.on("console", lambda m: m.type == "error" and p.errors.append(f"console: {m.text} ({m.location.get('url')})"))
            p.on("pageerror", lambda e: p.errors.append(f"script: {e}"))
            p.goto(url)
            p.wait_for_load_state("networkidle")
            return p

        def done(p: Page, what: str) -> None:
            for frame in p.frames:
                p.errors += [f"CSP: {v}" for v in frame.evaluate("window.__csp || []")]
            check(not p.errors, f"{what}: no console errors, script errors or CSP violations")
            problems.extend(p.errors)
            p.context.close()

        backgrounds = {}
        for width, height, scheme, lang in ((1200, 760, "light", "en-US"), (1200, 760, "dark", "de-DE"),
                                            (390, 844, "dark", "de-DE"), (390, 844, "light", "en-US")):
            what = f"{width} px, {scheme}, {lang}"
            print(f"==> {what}")
            p = page(width, height, scheme, lang)
            if shots and (width, scheme, lang) == (390, "dark", "de-DE"):
                # Before anything scrolls the page or the client list
                p.screenshot(path=OUT / "mobile-dark.png", full_page=True)
            header(p, lang, width < 600)
            backgrounds[scheme] = p.evaluate("getComputedStyle(document.body).backgroundColor")
            check(p.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{lang}: nothing wider than {width} px")
            # The page swaps in a fresh copy in place; with the request in flight the button spins
            with p.expect_response(lambda r: "refresh=1" in r.url):
                p.locator("#refresh").click()
            p.wait_for_function("!document.querySelector('#refresh.busy')")
            check(p.locator("header .stamp").count() == 1 and p.url == base + INGRESS, f"{lang}: refresh stays on the page")
            if shots and (width, scheme) == (1200, "light"):
                p.screenshot(path=OUT / "overview.png")
                p.locator("#clients").scroll_into_view_if_needed()
                p.locator(".chip[data-show=active]").click()
                p.locator(".card", has=p.locator("#clients")).screenshot(path=OUT / "clients.png")
                p.locator(".chip[data-show=all]").click()
            p.locator("tr[data-addr='192.0.2.31'] button.edit").click()
            check(p.locator("dialog#edit[open]").count() == 1, f"{lang}: edit dialog opens")
            if shots and (width, scheme) == (1200, "light"):
                p.wait_for_timeout(300)
                p.screenshot(path=OUT / "edit.png")
            p.locator("#edit-cancel").click()
            done(p, what)
        check(backgrounds["light"] != backgrounds["dark"], f"light and dark differ ({backgrounds})")

        print("==> In Home Assistant's frame")
        p = page(1200, 760, "light", "en-US", base + "/")
        frame = p.frame_locator("#ingress")
        frame.locator("header h1").wait_for()
        check(frame.locator("header h1").text_content() == "NTP server", "page shown in the ingress frame")
        frame.locator(f'header a[href="/config/app/{SLUG}/config"]').click()
        p.wait_for_url(f"**/config/app/{SLUG}/config")
        check(p.url == f"{base}/config/app/{SLUG}/config", "settings open outside the frame")
        done(p, "frame")

        # The browser asks for English; the page follows Home Assistant's language
        # (share/ha-app/ha-lang.js): one reload when it changes, none when not
        print("==> In Home Assistant's language, not the browser's")
        context = browser.new_context(viewport={"width": 1200, "height": 760}, locale="en-US",
                                      extra_http_headers={"Accept-Language": "en"})
        context.add_init_script(WATCH_CSP)
        p = context.new_page()
        p.errors = []
        p.on("console", lambda m: m.type == "error" and p.errors.append(f"console: {m.text} ({m.location.get('url')})"))
        p.on("pageerror", lambda e: p.errors.append(f"script: {e}"))
        for ha_lang, lang, expected in (("de", "de", 2), ("de", "de", 1), ("en-GB", "en", 2), ("fr", "en", 2),
                                        ("fr", "en", 1)):
            where = f"Home Assistant in {ha_lang}"
            app, loads = in_home_assistant(p, base, ha_lang)
            shown = app.evaluate("[document.documentElement.lang, getComputedStyle(document.documentElement).visibility]")
            check(shown == [lang, "visible"], f"{where}: page in {lang} and shown ({shown})")
            check(loads == expected, f"{where}: loaded {expected} time(s) ({loads})")
            header(app, {"de": "de-DE", "en": "en-US"}[lang], False)
            held = [(k["value"], k["path"]) for k in context.cookies() if k["name"] == "ha_lang"]
            check(held == [(ha_lang.split("-")[0], INGRESS)], f"{where}: cookie for the ingress path ({held})")
        done(p, "language")
        browser.close()


def in_home_assistant(p: Page, base: str, lang: str) -> tuple[Frame, int]:
    """Opens the page in a stand-in for Home Assistant's frontend in that language;
    the app's frame and how often it loaded."""
    loads: list[str] = []

    def seen(frame: Frame) -> None:
        if frame.parent_frame is not None and frame.url.startswith(base + INGRESS):
            loads.append(frame.url)

    p.on("framenavigated", seen)
    p.goto(f"{base}/?lang={lang}")
    p.wait_for_timeout(1500)  # room for another reload, which must not come
    p.remove_listener("framenavigated", seen)
    app = next(f for f in p.frames if f.parent_frame is not None)
    app.locator("body > header h1").wait_for()
    return app, len(loads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="check only, take no screenshots")
    parser.add_argument("--image", default="chrony-mqtt:check", help="the built image (default chrony-mqtt:check)")
    parser.add_argument("--build", action="store_true", help="build the image first")
    args = parser.parse_args()
    if args.build:
        subprocess.run(["docker", "build", "-q", "--build-arg", f"BUILD_VERSION={VERSION}", "-t", args.image,
                        str(ROOT / "chrony_ntp")], check=True, stdout=subprocess.DEVNULL)
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
    try:
        run(args.image, not args.check)
    finally:
        if problems:
            print(docker("logs", APP, check_=False))
        cleanup()
    if problems:
        print("FAILED:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("Page check passed." + ("" if args.check else f" Screenshots in {OUT.relative_to(ROOT)}"))


if __name__ == "__main__":
    main()
