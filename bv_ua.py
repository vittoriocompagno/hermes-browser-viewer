"""Client-hint attacher of ONE managed headless browser: a detached process that lives as long as the browser.

chrome-headless-shell names "HeadlessChrome" in its client hints (Sec-CH-UA, navigator.userAgentData) and
--user-agent blanks the high-entropy ones (full version list, architecture, bitness) on every engine. There is
no switch for either, so this process holds one idle CDP connection and:
  * pauses every new target before it runs (Target.setAutoAttach + waitForDebuggerOnStart), gives it
    Emulation.setUserAgentOverride with the metadata of the same Chromium with a window, then lets it go;
  * pauses every request (Fetch) and rewrites the hints of the ones that still carry the browser's own values:
    a navigation started together with its tab (Target.createTarget(url), a target=_blank link) builds its
    headers in the browser process before any override can reach the new target. A request that already has
    the right values, or that another CDP client gave a user agent of its own, is passed through untouched.

Fetch hands the headers over sorted by name and sends them in the order they are given back, so a rewritten
request is put back in the order Chromium itself uses (HINT_ORDER); the others are continued as they are.

Standard library only and synchronous (about 7 MB of memory; asyncio + aiohttp would be 27 MB per browser).
The configuration arrives as one JSON line on stdin, "ready" is printed once the first page may navigate.
When the browser goes away the socket closes and the process exits; if this process dies, Chromium releases
everything the connection paused, so pages keep working (with the hints of the browser itself).
"""
from __future__ import annotations

import base64
import json
import os
import platform
import socket
import struct
import sys
import time
from urllib.parse import urlsplit

INIT_TIMEOUT = 10.0     # seconds for every step of the set-up
MAX_LOG_LINES = 200     # ua.log must not grow with the number of requests
# the order Chromium writes the client hints of a navigation in (the order of its own list of hints)
HINT_ORDER = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-full-version", "sec-ch-ua-arch", "sec-ch-ua-platform",
              "sec-ch-ua-platform-version", "sec-ch-ua-model", "sec-ch-ua-bitness", "sec-ch-ua-wow64",
              "sec-ch-ua-full-version-list", "sec-ch-ua-form-factors")


class WebSocket:
    """Minimal RFC 6455 client for a loopback CDP endpoint: text messages, no extensions."""

    def __init__(self, url: str, timeout: float):
        u = urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = bytearray()
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("the browser refused the WebSocket: " + head.split(b"\r\n", 1)[0].decode("latin-1"))
        self.buf = bytearray(rest)

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("browser disconnected")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else
                                         bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                         bytes([0x80 | 127]) + struct.pack(">Q", n))
        # a client frame must be masked; the key is free to choose and a zero key leaves the payload as it is
        self.sock.sendall(head + b"\0\0\0\0" + payload)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def recv(self) -> str:
        """The next text message; EOFError when the browser closed the connection."""
        parts = []
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4) if b1 & 0x80 else None
            data = self._take(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise EOFError("browser closed the connection")
            if opcode == 0x9:
                self._frame(0xA, data)
            elif opcode in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")


def brand_list(native, major: int):
    """The brands of the same Chromium with a window. The GREASE brand and its version come from the browser
    itself (they change with every major); only its place is computed: Chromium shuffles the list by major,
    so with HeadlessChrome simply removed the two brands left are in the wrong order for four majors out of six."""
    brands = [b for b in native if b["brand"] != "HeadlessChrome"]
    if len(brands) == len(native) or len(brands) != 2:
        return brands
    chromium = next((b for b in brands if b["brand"] == "Chromium"), None)
    if not chromium:
        return brands
    grease = next(b for b in brands if b is not chromium)
    return [chromium, grease] if major % 2 else [grease, chromium]      # the permutation of {grease, Chromium}


def hint(brands) -> str:
    return ", ".join(json.dumps(b["brand"]) + ";v=" + json.dumps(b["version"]) for b in brands)


def platform_version() -> str:
    if sys.platform != "darwin":
        return ""                           # what Chromium reports on Linux
    parts = (platform.mac_ver()[0] or "0").split(".")
    return ".".join((parts + ["0", "0"])[:3])       # Chrome always sends three components: 14.5 -> 14.5.0


class Attacher:
    def __init__(self, ws: WebSocket, config):
        self.ws = ws
        self.config = config
        self.mid = 0
        self.pending = {}           # id -> callback(reply) of a command whose answer matters
        self.pages = {}             # targetId -> sessionId of the page targets that have their override
        self.busy = 0               # targets attached and not yet released
        self.override = None
        self.fix = {}
        self.native_hint = ""
        self.logged = 0

    def log(self, text: str) -> None:
        self.logged += 1
        if self.logged <= MAX_LOG_LINES:
            print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, file=sys.stderr, flush=True)

    def send(self, method, sid=None, then=None, **params) -> int:
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        if then:
            self.pending[self.mid] = then
        self.ws.send(json.dumps(msg))
        return self.mid

    def step(self) -> None:
        d = json.loads(self.ws.recv())
        if "id" in d:
            then = self.pending.pop(d["id"], None)
            if then:
                then(d)
            return
        try:
            method = d.get("method")
            if method == "Fetch.requestPaused":
                self.paused(d["params"], d.get("sessionId"))
            elif method == "Target.attachedToTarget" and self.override:
                self.attached(d["params"])
            elif method == "Target.detachedFromTarget":
                sid = d["params"]["sessionId"]
                self.pages = {t: s for t, s in self.pages.items() if s != sid}
        except (KeyError, TypeError, ValueError) as exc:        # one odd event must not end the override of all tabs
            self.log(f"{d.get('method')}: {type(exc).__name__}: {exc}")

    def call(self, method, sid=None, **params):
        """Send and wait for the answer (set-up only); events that arrive meanwhile are handled as usual."""
        box = []
        self.send(method, sid, then=box.append, **params)
        while not box:
            self.step()
        if "error" in box[0]:
            raise RuntimeError(f"{method}: {box[0]['error']}")
        return box[0].get("result", {})

    # ------------------------------------------------------------------ targets
    def attached(self, params) -> None:
        sid, info = params["sessionId"], params["targetInfo"]
        kind = info.get("type")
        self.busy += 1

        def release(_reply=None):
            # auto-attach is not recursive: each tab, page, frame and worker has to watch its own children.
            # The target is released whatever happened: a page must never stay paused because of us
            def watched(_r):
                self.busy -= 1
            self.send("Target.setAutoAttach", sid, then=watched, autoAttach=True, waitForDebuggerOnStart=True, flatten=True)
            self.send("Runtime.runIfWaitingForDebugger", sid)
            if kind == "page":
                self.pages[info["targetId"]] = sid

        if kind == "tab":
            return release()

        def emulated(reply):
            if "error" in reply:        # workers have no Emulation domain: Network carries the same override
                self.send("Network.setUserAgentOverride", sid, then=release, **self.override)
            else:
                release()
        self.send("Emulation.setUserAgentOverride", sid, then=emulated, **self.override)

    # ------------------------------------------------------------------ requests
    def paused(self, params, sid) -> None:
        headers = params["request"]["headers"]
        low = {k.lower(): v for k, v in headers.items()}
        # ours to fix only while the request speaks as the browser itself does: a user agent or hints another CDP
        # client set on purpose (device emulation) are left as they are
        ours = (low.get("user-agent") == self.config["userAgent"]
                and low.get("sec-ch-ua", self.native_hint) in (self.native_hint, self.fix["sec-ch-ua"]))
        add_language = "accept-language" in self.fix and "accept-language" not in low
        if not ours or not (add_language or any(low[k] != v for k, v in self.fix.items() if k in low)):
            self.send("Fetch.continueRequest", sid, requestId=params["requestId"])
            return
        fixed = {k: self.fix.get(k.lower(), v) for k, v in headers.items()}
        names = sorted((k for k in fixed if k.lower() in HINT_ORDER), key=lambda k: HINT_ORDER.index(k.lower()))
        rest = [k for k in fixed if k.lower() not in HINT_ORDER and k.lower() != "accept"]
        out = [{"name": k, "value": fixed[k]} for k in names + rest]
        if add_language:        # the shell would add its own later, without the q-weights of a real Chrome
            out.append({"name": "Accept-Language", "value": self.fix["accept-language"]})
        out += [{"name": k, "value": v} for k, v in fixed.items() if k.lower() == "accept"]
        self.send("Fetch.continueRequest", sid, requestId=params["requestId"], headers=out)

    # ------------------------------------------------------------------ set-up
    def probe_brands(self):
        """navigator.userAgentData.brands as this browser builds them. It exists in secure contexts only: the probe
        tab loads the browser's own loopback CDP endpoint, never a remote site, and is closed again."""
        probe = self.call("Target.createTarget", url="about:blank")["targetId"]
        try:
            sid = self.call("Target.attachToTarget", targetId=probe, flatten=True)["sessionId"]
            self.call("Page.navigate", sid, url=self.config["cdp"] + "/json/version")
            deadline = time.monotonic() + INIT_TIMEOUT
            while time.monotonic() < deadline:
                r = self.call("Runtime.evaluate", sid, expression="navigator.userAgentData?.brands", returnByValue=True)
                if r["result"].get("value"):
                    return r["result"]["value"]
                time.sleep(0.01)
            raise RuntimeError("the browser exposes no navigator.userAgentData")
        finally:
            self.call("Target.closeTarget", targetId=probe)

    def initialize(self) -> None:
        config = self.config
        version = self.call("Browser.getVersion")["product"].split("/")[-1]
        first = next((t["targetId"] for t in self.call("Target.getTargets")["targetInfos"] if t["type"] == "page"), None)
        native = self.probe_brands()
        brands = brand_list(native, int(version.split(".")[0]))
        full = [{"brand": b["brand"], "version": version if b["brand"] in ("Chromium", "Google Chrome", "Microsoft Edge")
                 else b["version"] + ".0.0.0"} for b in brands]
        mac = sys.platform == "darwin"
        metadata = {
            "brands": brands, "fullVersionList": full, "fullVersion": version,
            "platform": "macOS" if mac else "Linux", "platformVersion": platform_version(),
            "architecture": "arm" if platform.machine().lower().startswith(("arm", "aarch")) else "x86",
            "bitness": "64" if sys.maxsize > 2 ** 32 else "32", "model": "", "mobile": False, "wow64": False,
        }
        # navigator.platform is not overridden: the browser's own value is the true one
        self.override = {"userAgent": config["userAgent"], "userAgentMetadata": metadata}
        self.native_hint = hint(native)
        self.fix = {"sec-ch-ua": hint(brands), "sec-ch-ua-full-version-list": hint(full),
                    "sec-ch-ua-mobile": "?0", "sec-ch-ua-wow64": "?0"}
        if config.get("acceptLanguage"):
            # only the shell needs it (it sends the list of --accept-lang without q-weights); Chromium is left
            # alone because an overridden Accept-Language leaves its place at the end of the headers.
            # CDP adds the weights of the header itself: given here they would end up in navigator.languages
            self.override["acceptLanguage"] = ",".join(s.split(";", 1)[0].strip() for s in config["acceptLanguage"].split(","))
            self.fix["accept-language"] = config["acceptLanguage"]
        for name, key in (("platform", "platform"), ("platform-version", "platformVersion"), ("arch", "architecture"),
                          ("bitness", "bitness"), ("model", "model"), ("full-version", "fullVersion")):
            self.fix["sec-ch-ua-" + name] = json.dumps(metadata[key])
        self.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        # pages are reached through their tab target, so that a new one is paused before its first navigation
        self.call("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=True, flatten=True,
                  filter=[{"type": "browser", "exclude": True}, {"type": "page", "exclude": True}, {}])
        self.call("Browser.getVersion")                 # the tabs that were open are attached by now
        while self.busy:
            self.step()
        if config.get("url") and config["url"] != "about:blank" and first in self.pages:
            self.call("Page.navigate", self.pages[first], url=config["url"])


def main() -> int:
    config = json.loads(sys.stdin.readline())
    ws = WebSocket(config["ws"], INIT_TIMEOUT)
    attacher = Attacher(ws, config)
    try:
        attacher.initialize()       # a browser that does not answer within INIT_TIMEOUT ends it (socket timeout)
        print("ready", flush=True)
        ws.sock.settimeout(None)
        while True:
            attacher.step()
    except EOFError:
        return 0                    # the browser is gone: so are we
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        attacher.log(f"stopped: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
