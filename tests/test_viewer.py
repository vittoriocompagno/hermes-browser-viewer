"""Integration tests for the browser-viewer plugin: real Chromium, real server, real WebSocket clients.

Run (from the plugin directory):
    uv run --with aiohttp python -m unittest -v tests.test_viewer

Everything runs on scratch ports with a throw-away data dir: a browser or viewer you already use
is never touched.
"""
import asyncio
import atexit
import http.server
import importlib.util
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="bv-test-")
os.environ["BROWSER_VIEWER_HOME"] = TMP
os.environ["BV_CLOSE_GRACE"] = "1"
os.environ["BV_AUTH_RECHECK"] = "0.5"      # open streams re-check their credential twice a second (5 s in real life)
atexit.register(shutil.rmtree, TMP, True)
import bv_core  # noqa: E402

PORT = 9391
BASE = f"http://127.0.0.1:{PORT}"
HDR = struct.Struct(">BHHHH")


def data_url(html: str) -> str:
    return "data:text/html," + urllib.parse.quote(html, safe="")


ANIM = data_url("<body style='margin:0'><h1 id=n>0</h1><script>setInterval(()=>{n.textContent=(performance.now()|0);"
                "document.body.style.background='hsl('+((performance.now()/8)%360)+',70%,60%)'},30)</script>")
FORM = data_url("<body style='margin:0'><input id=f style='position:fixed;left:40px;top:40px;width:320px;height:36px;font-size:20px'>"
                "<div id=b style='position:fixed;right:0;top:0;width:100px;height:100px'></div>"
                "<script>setInterval(()=>{b.style.background='hsl('+((Date.now()/8)%360)+',70%,60%)'},30)</script>")
NOISE = data_url("<body style='margin:0'><canvas id=c width=1100 height=700></canvas><script>const x=c.getContext('2d');"
                 "(function f(){for(let i=0;i<400;i++){x.fillStyle='rgb('+(Math.random()*255|0)+','+(Math.random()*255|0)+','+(Math.random()*255|0)+')';"
                 "x.fillRect(Math.random()*1100,Math.random()*700,30,30)}requestAnimationFrame(f)})()</script>")


class Cdp:
    """Minimal browser-level CDP client used by the tests to drive the page like an agent would."""

    def __init__(self, cdp_http):
        self.cdp = cdp_http

    async def __aenter__(self):
        self.s = aiohttp.ClientSession()
        ver = await (await self.s.get(self.cdp + "/json/version")).json()
        self.ws = await self.s.ws_connect(ver["webSocketDebuggerUrl"], max_msg_size=2 ** 26)
        self.mid, self.pend = 0, {}
        self.task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await self.ws.close()
        await self.s.close()

    async def _pump(self):
        async for m in self.ws:
            d = json.loads(m.data)
            f = self.pend.get(d.get("id"))
            if f and not f.done():
                f.set_result(d)

    async def call(self, method, sid=None, **p):
        self.mid += 1
        f = asyncio.get_running_loop().create_future()
        self.pend[self.mid] = f
        msg = {"id": self.mid, "method": method, "params": p}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send_json(msg)
        d = await asyncio.wait_for(f, 10)
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error']}")
        return d["result"]

    async def new_tab(self, url):
        return (await self.call("Target.createTarget", url=url))["targetId"]

    async def close_tab(self, tid):
        await self.call("Target.closeTarget", targetId=tid)

    async def navigate(self, tid, url):
        sid = (await self.call("Target.attachToTarget", targetId=tid, flatten=True))["sessionId"]
        await self.call("Page.navigate", sid, url=url)
        await self.call("Target.detachFromTarget", sessionId=sid)

    async def evaluate(self, tid, expr):
        sid = (await self.call("Target.attachToTarget", targetId=tid, flatten=True))["sessionId"]
        try:
            r = await self.call("Runtime.evaluate", sid, expression=expr, returnByValue=True)
            return r["result"].get("value")
        finally:
            await self.call("Target.detachFromTarget", sessionId=sid)


class Viewer:
    """A viewer window as the web page behaves: acks frames after 'decoding', can stay silent to emulate a slow client."""

    def __init__(self, instance, token=None, ack=True):
        self.instance, self.token, self.ack = instance, token or bv_core.get_token(), ack
        self.frames, self.msgs, self.tabs, self.active, self.hello = [], [], [], None, None

    async def __aenter__(self):
        self.s = aiohttp.ClientSession()
        self.ws = await self.s.ws_connect(f"{BASE}/ws?i={self.instance}", headers=tok_header(self.token), max_msg_size=2 ** 24)
        self.task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await self.ws.close()
        await self.s.close()

    async def _read(self):
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.BINARY:
                kind, idx, w, h, fid = HDR.unpack(m.data[:9])
                self.frames.append({"t": time.monotonic(), "idx": idx, "w": w, "h": h, "fid": fid, "n": len(m.data) - 9, "magic": m.data[9:11]})
                if self.ack:
                    await self.ws.send_json({"t": "ack", "f": fid})
            else:
                j = json.loads(m.data)
                self.msgs.append(j)
                if j["t"] == "tabs":
                    self.tabs, self.active = j["tabs"], j["active"]
                elif j["t"] == "hello":
                    self.hello = j

    async def view(self, mode="single", tab=None, q="auto"):
        await self.ws.send_json({"t": "view", "mode": mode, "tab": tab, "q": q})

    async def send(self, **m):
        await self.ws.send_json(m)

    def idx_of(self, tid):
        return next((t["idx"] for t in self.tabs if t["id"] == tid), None)

    def count(self, idx=None, since=0.0):
        return sum(1 for f in self.frames if f["t"] >= since and (idx is None or f["idx"] == idx))


async def until(cond, timeout=6.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = cond()
        if r:
            return r
        await asyncio.sleep(step)
    return False


def tok_header(token=None):
    return {"X-BV-Token": token or bv_core.get_token()}


def procs_with(arg):
    """pids whose command line contains `arg` (a /proc scan: pgrep -f would also match its own shell)."""
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and int(d.name) != os.getpid():
            try:
                if arg.encode() in (d / "cmdline").read_bytes():
                    out.append(int(d.name))
            except OSError:
                pass
    return out


async def stats():
    async with aiohttp.ClientSession() as s:
        return await (await s.get(f"{BASE}/api/stats", headers=tok_header())).json()


def _stop_instance(name):
    try:
        bv_core.stop_instance(name, purge=True)
    except bv_core.ViewerError:
        pass


class ViewerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # cleanups are registered as each resource appears, so a failure half-way still tears everything down
        cls.addClassCleanup(shutil.rmtree, TMP, True)
        cls.inst = bv_core.start_instance("t1", size="1024,700")
        cls.addClassCleanup(_stop_instance, "t1")
        cls.inst2 = bv_core.start_instance("t2", size="800,600")
        cls.addClassCleanup(_stop_instance, "t2")
        cls.addClassCleanup(bv_core.stop_server)
        cls.srv = bv_core.start_server(port=PORT, hosts=["127.0.0.1"], idle_minutes=0)
    # ---------------------------------------------------------------- auth
    async def test_auth(self):
        tok = bv_core.get_token()
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{BASE}/")
            self.assertEqual(r.status, 200)                              # the static shell needs no credential
            self.assertIn("no-store", r.headers.get("Cache-Control", ""))
            self.assertNotIn(tok, await r.text(), "the page itself must never contain the token")
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers=tok_header("wrong"))).status, 403)
            self.assertEqual((await s.get(f"{BASE}/api/stats")).status, 403)
            self.assertEqual((await s.get(f"{BASE}/api/stats?k={tok}")).status, 403, "a master token in a URL is not accepted")
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers=tok_header())).status, 200)
            self.assertEqual((await s.get(f"{BASE}/healthz")).status, 200)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await s.ws_connect(f"{BASE}/ws?i=t1", headers=tok_header("wrong"))
            self.assertEqual(cm.exception.status, 403)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:   # cross-site page with a valid token
                await s.ws_connect(f"{BASE}/ws?i=t1", headers={**tok_header(), "Origin": "http://evil.example"})
            self.assertEqual(cm.exception.status, 403)

    async def test_token_rotation_without_restart(self):
        old = bv_core.get_token()
        new = bv_core.get_token(rotate=True)
        async with aiohttp.ClientSession() as s:
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers=tok_header(old))).status, 403)
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers=tok_header(new))).status, 200)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await s.ws_connect(f"{BASE}/ws?i=t1", headers=tok_header(old))
            self.assertEqual(cm.exception.status, 403)

    async def _until_closed(self, ws, timeout=8):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            m = await ws.receive(timeout=timeout)
            if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                return True
        return False

    async def test_open_stream_is_closed_when_the_master_secret_is_rotated(self):
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"{BASE}/ws?i=t1", headers=tok_header())
            self.assertEqual((await ws.receive(timeout=5)).type, aiohttp.WSMsgType.TEXT)
            bv_core.get_token(rotate=True)
            self.assertTrue(await self._until_closed(ws), "a signed-out browser kept streaming")
            self.assertEqual(ws.close_code, 4401)

    async def test_open_stream_is_closed_when_the_session_cookie_expires(self):
        import bv_server
        exp = int(time.time()) + 2
        cookie = f"{exp}.n.{bv_server._sign(exp, 'n', bv_core.get_token())}"
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"{BASE}/ws?i=t1", headers={"Cookie": f"bvs_{PORT}={cookie}"})
            self.assertEqual((await ws.receive(timeout=5)).type, aiohttp.WSMsgType.TEXT)
            self.assertTrue(await self._until_closed(ws), "an expired session kept streaming")
            self.assertEqual(ws.close_code, 4401)

    async def test_origin_must_match_scheme_and_host_and_the_host_must_be_one_we_serve(self):
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{BASE}/healthz", headers={"Host": "evil.example"})
            self.assertEqual(r.status, 421, "a host name this server does not serve is refused (DNS rebinding)")
            self.assertEqual((await s.get(f"{BASE}/healthz", headers={"Host": f"localhost:{PORT}"})).status, 200)
            for origin in (f"https://127.0.0.1:{PORT}", f"http://127.0.0.1:{PORT + 1}", "null", "http://127.0.0.1"):
                r = await s.post(f"{BASE}/api/session", json={"b": bv_core.new_bootstrap()}, headers={"Origin": origin})
                self.assertEqual(r.status, 403, origin)
                with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                    await s.ws_connect(f"{BASE}/ws?i=t1", headers={**tok_header(), "Origin": origin})
                self.assertEqual(cm.exception.status, 403, origin)
            ok = await s.post(f"{BASE}/api/session", json={"b": bv_core.new_bootstrap()}, headers={"Origin": BASE})
            self.assertEqual(ok.status, 200)

    async def test_security_headers_on_pages_and_errors(self):
        async with aiohttp.ClientSession() as s:
            for path in ("/", "/api/stats", "/healthz"):
                r = await s.get(f"{BASE}{path}")
                h = r.headers
                self.assertEqual(h.get("X-Content-Type-Options"), "nosniff", path)
                self.assertEqual(h.get("X-Frame-Options"), "DENY", path)
                self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""), path)
                self.assertEqual(h.get("Referrer-Policy"), "no-referrer", path)
                self.assertNotIn("Strict-Transport-Security", h, "HSTS would pin the whole host name")

    async def test_non_utf8_credentials_are_403_never_500(self):
        for head in (b"X-BV-Token: \xff\xfe\xfd", f"Cookie: bvs_{PORT}=\xff.\xff.\xff".encode("latin-1")):
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"GET /api/stats HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % PORT + head + b"\r\nConnection: close\r\n\r\n")
            await writer.drain()
            line = (await reader.readline()).decode("latin-1")
            writer.close()
            self.assertIn(" 403 ", line, head)
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{BASE}/api/session", data='{"b": "\\ud800"}', headers={"Origin": BASE, "Content-Type": "application/json"})
            self.assertEqual(r.status, 403)

    async def test_login_link_works_in_a_real_browser_under_the_csp(self):
        (link,) = bv_core.links("t1")
        async with Cdp(self.inst2["cdp"]) as b:                 # the second managed browser plays the user's device
            tid = await b.new_tab(link)
            try:
                dot = lambda: b.evaluate(tid, "document.getElementById('st').classList.contains('on')")
                drawn = lambda: b.evaluate(tid, "(() => { const d = document.getElementById('main').getContext('2d').getImageData(0, 0, 64, 64).data; return d.some(v => v !== 0); })()")
                for _ in range(80):
                    if await dot() and await drawn():
                        break
                    await asyncio.sleep(0.15)
                self.assertTrue(await dot(), "the stream never connected: is the CSP blocking the page?")
                self.assertTrue(await drawn(), "the canvas stayed empty")
                href = await b.evaluate(tid, "location.href")
                self.assertNotIn("b=", href, "the one-time token must leave the address bar")
            finally:
                await b.close_tab(tid)

    async def test_every_screencast_ack_that_is_owed_is_sent_on_release(self):
        import bv_server
        hub = bv_server.Hub("x", "http://127.0.0.1:1")
        sent = []

        async def fire(method, sid=None, **params):
            sent.append((method, sid, params))
        hub.fire = fire

        class Busy:                                   # a watcher with no room: the browser has to wait
            WINDOW = 2
            inflight = [1, 2]
            pending = []

            def has_capacity(self):
                return False

            def offer(self, *a):
                pass
        hub.subscribers = lambda tid: [Busy()]
        st = bv_server.Stream(hub, "T")
        st.sid = "S"
        hub.streams["T"] = st
        frame = lambda sid: {"metadata": {"deviceWidth": 4, "deviceHeight": 4}, "data": "eA==", "sessionId": sid}
        try:
            hub._on_cast_frame(st, frame(1))
            timer = st.hold_timer
            hub._on_cast_frame(st, frame(2))
            hub._on_cast_frame(st, frame(3))
            self.assertIs(st.hold_timer, timer, "a later frame must not replace the running hold timer")
            self.assertEqual(st.holds, [1, 2, 3])
            self.assertEqual(sent, [], "no ack while every watcher is busy")
            hub._release_hold(st)
            await asyncio.sleep(0.05)
            self.assertEqual([p["sessionId"] for m, sid, p in sent if m == "Page.screencastFrameAck"], [1, 2, 3],
                             "frames whose ack was never sent shrink Chromium's window for good")
        finally:
            await hub.http.close()

    async def test_a_window_that_joins_a_static_page_gets_a_picture_at_once(self):
        static = data_url("<title>static</title><body style='margin:0;background:#fde68a'><h1>nothing moves here</h1>")
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(static)
            try:
                async with Viewer("t1") as first:
                    self.assertTrue(await until(lambda: first.active == tid and first.count(first.idx_of(tid)) >= 1, 8))
                    async with Viewer("t1") as second:       # a reload, a second device: the page itself never repaints
                        self.assertTrue(await until(lambda: second.active == tid and second.count(second.idx_of(tid)) >= 1, 3),
                                        "the new window stayed blank until the page changed")
            finally:
                await b.close_tab(tid)

    # ---------------------------------------------------------------- one-time login link -> session cookie
    @staticmethod
    def _b(link):
        return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["b"][0]

    @staticmethod
    def _jar_session():
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))      # the host is an IP address

    async def test_login_link_is_single_use_and_signs_the_browser_in(self):
        (link,) = bv_core.links("t1")
        self.assertIn("?b=", link)
        self.assertNotIn(bv_core.get_token(), link, "the master token must never be in a link")
        b = self._b(link)
        async with self._jar_session() as s:
            self.assertEqual((await s.get(f"{BASE}/api/auth")).status, 403)
            self.assertEqual((await s.get(f"{BASE}/?b={b}")).status, 200)            # merely opening the link uses nothing up
            r = await s.post(f"{BASE}/api/session", json={"b": b}, headers={"Origin": BASE})
            self.assertEqual(r.status, 200)
            cookie = r.headers.getone("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertTrue(cookie.startswith(f"bvs_{PORT}="))
            self.assertNotIn("Secure", cookie, "over plain HTTP the cookie cannot carry Secure (the browser would drop it)")
            self.assertEqual((await s.get(f"{BASE}/api/auth")).status, 200)
            self.assertEqual((await s.get(f"{BASE}/api/stats")).status, 200)        # cookie only, no ?k=
            ws = await s.ws_connect(f"{BASE}/ws?i=t1")
            self.assertEqual((await ws.receive(timeout=5)).type, aiohttp.WSMsgType.TEXT)
            await ws.close()
        async with self._jar_session() as other:                                     # the same link a second time
            r = await other.post(f"{BASE}/api/session", json={"b": b}, headers={"Origin": BASE})
            self.assertEqual(r.status, 403)
            self.assertEqual((await other.get(f"{BASE}/api/stats")).status, 403)

    async def test_login_refuses_foreign_origin_bad_bodies_and_expired_tokens(self):
        async with self._jar_session() as s:
            b = bv_core.new_bootstrap()
            for headers in ({}, {"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1:1"}):
                r = await s.post(f"{BASE}/api/session", json={"b": b}, headers=headers)
                self.assertEqual(r.status, 403, headers)
            self.assertEqual((await s.post(f"{BASE}/api/session", data="not json", headers={"Origin": BASE})).status, 400)
            for body in ({"b": 5}, {"b": ""}, {"x": 1}, [1], "str", {"b": "x" * 500}):
                r = await s.post(f"{BASE}/api/session", json=body, headers={"Origin": BASE})
                self.assertEqual(r.status, 403, body)
            r = await s.post(f"{BASE}/api/session", json={"b": b}, headers={"Origin": BASE})
            self.assertEqual(r.status, 200, "a refused request must not use the token up")
            expired = bv_core.new_bootstrap(ttl=-1)
            r = await s.post(f"{BASE}/api/session", json={"b": expired}, headers={"Origin": BASE})
            self.assertEqual(r.status, 403)

    async def test_forged_and_expired_cookies_and_rotation_sign_everyone_out(self):
        import bv_server
        master = bv_core.get_token()
        exp = int(time.time()) - 5
        cookies = {
            "garbage": "x",
            "wrong-signature": f"{int(time.time()) + 3600}.n.{'0' * 64}",
            "expired": f"{exp}.n.{bv_server._sign(exp, 'n', master)}",
            "non-ascii": "é.é.é",
        }
        async with aiohttp.ClientSession() as s:
            for label, value in cookies.items():
                r = await s.get(f"{BASE}/api/stats", headers={"Cookie": f"bvs_{PORT}={value}"})
                self.assertEqual(r.status, 403, label)
            good = bv_server.make_session(master)
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers={"Cookie": f"bvs_{PORT}={good}"})).status, 200)
            unused = bv_core.new_bootstrap()
            bv_core.get_token(rotate=True)
            self.assertEqual((await s.get(f"{BASE}/api/stats", headers={"Cookie": f"bvs_{PORT}={good}"})).status, 403)
            r = await s.post(f"{BASE}/api/session", json={"b": unused}, headers={"Origin": BASE})
            self.assertEqual(r.status, 403, "rotating must also drop the links that were not used yet")

    # ---------------------------------------------------------------- streaming + input
    async def test_stream_frames_and_input(self):
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(FORM)
            try:
                async with Viewer("t1") as v:
                    self.assertTrue(await until(lambda: v.hello and v.tabs), 5)
                    self.assertTrue(await until(lambda: v.active == tid and v.count(v.idx_of(tid)) > 3, 8), "no frames from the active tab")
                    f = v.frames[-1]
                    self.assertEqual(f["magic"], b"\xff\xd8", "frames must be raw JPEG (binary, not base64)")
                    # click the input and type + paste; the page must really receive it
                    await v.send(t="mouse", tab=tid, type="mousePressed", x=100, y=58, button="left", clickCount=1)
                    await v.send(t="mouse", tab=tid, type="mouseReleased", x=100, y=58, button="left", clickCount=1)
                    for ch in "ciao":
                        await v.send(t="key", tab=tid, type="keyDown", key=ch, code="Key" + ch.upper(), vk=ord(ch.upper()), text=ch)
                        await v.send(t="key", tab=tid, type="keyUp", key=ch, code="Key" + ch.upper(), vk=ord(ch.upper()))
                    await v.send(t="text", tab=tid, text="@x.it")
                    await asyncio.sleep(0.4)
                    got = await b.evaluate(tid, "document.getElementById('f').value")
                    self.assertEqual(got, "ciao@x.it")
                    await v.send(t="key", tab=tid, type="keyDown", key="a", code="KeyA", vk=65, commands=["selectAll"])
                    await v.send(t="key", tab=tid, type="keyDown", key="Backspace", code="Backspace", vk=8)
                    await asyncio.sleep(0.3)
                    self.assertEqual(await b.evaluate(tid, "document.getElementById('f').value"), "")
            finally:
                await b.close_tab(tid)

    async def test_input_into_foreign_tab_is_refused(self):
        async with Cdp(self.inst["cdp"]) as b:
            a, c = await b.new_tab(FORM), await b.new_tab(FORM)
            try:
                async with Viewer("t1") as v:
                    await v.view(tab=a)
                    self.assertTrue(await until(lambda: v.count(v.idx_of(a)) > 1, 8))
                    # the window looks at tab 'a': typing aimed at 'c' must be ignored
                    await v.send(t="mouse", tab=c, type="mousePressed", x=100, y=58, button="left", clickCount=1)
                    await v.send(t="mouse", tab=c, type="mouseReleased", x=100, y=58, button="left", clickCount=1)
                    await v.send(t="text", tab=c, text="NOPE")
                    await asyncio.sleep(0.5)
                    self.assertEqual(await b.evaluate(c, "document.getElementById('f').value"), "")
            finally:
                await b.close_tab(a)
                await b.close_tab(c)

    # ---------------------------------------------------------------- following the agent
    async def test_follow_new_tab_and_navigation_of_hidden_tab(self):
        async with Cdp(self.inst["cdp"]) as b:
            a = await b.new_tab(ANIM)
            try:
                async with Viewer("t1") as v:
                    self.assertTrue(await until(lambda: v.tabs, 5))
                    t_new = await b.new_tab(ANIM)    # the agent opens a new tab: the view must follow
                    try:
                        self.assertTrue(await until(lambda: v.active == t_new, 6), "view did not follow the new tab")
                        mark_new = time.monotonic()
                        self.assertTrue(await until(lambda: v.count(v.idx_of(t_new), since=mark_new) > 3, 8), "no frames after following")
                        # 'a' is now hidden. The agent navigates in it: this was the original bug (frozen view)
                        await asyncio.sleep(1.2)
                        mark = time.monotonic()
                        await b.navigate(a, ANIM + "#again")
                        self.assertTrue(await until(lambda: v.active == a, 6), "view did not switch to the navigating hidden tab")
                        self.assertTrue(await until(lambda: v.count(v.idx_of(a), since=mark) > 3, 8), "hidden tab produced no frames")
                    finally:
                        await b.close_tab(t_new)
            finally:
                await b.close_tab(a)

    # ---------------------------------------------------------------- many windows
    async def test_many_windows_share_one_stream(self):
        async with Cdp(self.inst["cdp"]) as b:
            a, c = await b.new_tab(ANIM), await b.new_tab(ANIM)
            try:
                async with Viewer("t1") as w1, Viewer("t1") as w2, Viewer("t1") as w3:
                    for w in (w1, w2, w3):
                        self.assertTrue(await until(lambda: w.tabs, 5))
                    await w1.view(tab=a)
                    await w2.view(tab=a)
                    await w3.view(tab=c)
                    self.assertTrue(await until(lambda: all(w.count() > 5 for w in (w1, w2, w3)), 10), "every window must receive frames")
                    s = (await stats())[0]
                    watched = {a, c}
                    self.assertEqual(set(s["streams"]), watched, "one stream per watched tab, not per window")
                    self.assertEqual(len(s["clients"]), 3)
            finally:
                await b.close_tab(a)
                await b.close_tab(c)

    async def test_two_browsers_are_independent(self):
        async with Cdp(self.inst["cdp"]) as b1, Cdp(self.inst2["cdp"]) as b2:
            t1, t2 = await b1.new_tab(ANIM), await b2.new_tab(ANIM)
            try:
                async with Viewer("t1") as v1, Viewer("t2") as v2:
                    self.assertTrue(await until(lambda: v1.tabs and v2.tabs, 5))
                    self.assertIn(t1, [t["id"] for t in v1.tabs])
                    self.assertNotIn(t2, [t["id"] for t in v1.tabs])
                    self.assertIn(t2, [t["id"] for t in v2.tabs])
                    self.assertEqual(v1.hello["instance"], "t1")
                    self.assertEqual(sorted(v1.hello["instances"]), ["t1", "t2"])
                    self.assertTrue(await until(lambda: v1.count() > 3 and v2.count() > 3, 10))
            finally:
                await b1.close_tab(t1)
                await b2.close_tab(t2)

    async def test_mosaic_shows_hidden_tabs(self):
        async with Cdp(self.inst["cdp"]) as b:
            tabs = [await b.new_tab(ANIM + f"#{i}") for i in range(3)]
            try:
                async with Viewer("t1") as v:
                    await v.view(mode="mosaic")
                    self.assertTrue(await until(lambda: all(v.count(v.idx_of(t)) >= 1 for t in tabs), 10),
                                    "mosaic must deliver a frame for every tab, hidden ones included")
                    s = (await stats())[0]
                    self.assertTrue(all(st["mode"] == "poll" for st in s["streams"].values()))
                    big = max(f["w"] for f in v.frames)
                    self.assertGreater(big, 0)
            finally:
                for t in tabs:
                    await b.close_tab(t)

    # ---------------------------------------------------------------- speed / adaptivity
    async def test_slow_client_does_not_slow_a_fast_one_and_is_bounded(self):
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(NOISE)
            try:
                async with Viewer("t1", ack=True) as fast, Viewer("t1", ack=False) as slow:
                    await fast.view(tab=tid, q="low")
                    await slow.view(tab=tid, q="low")
                    self.assertTrue(await until(lambda: fast.count() > 3, 10))
                    t0 = time.monotonic()
                    n0 = fast.count()
                    await asyncio.sleep(2.0)
                    fast_rate = (fast.count() - n0) / (time.monotonic() - t0)
                    self.assertGreater(fast_rate, 8, f"fast client starved by a slow one ({fast_rate:.1f} fps)")
                    self.assertLessEqual(len(slow.frames), 2, "a client that never acks must not receive more than the send window")
                    s = (await stats())[0]
                    slow_stats = [c for c in s["clients"] if c["sent"] <= 2][0]
                    self.assertGreater(slow_stats["drops"], 0, "stale frames must be dropped, not queued")
            finally:
                await b.close_tab(tid)

    async def test_quality_levels_change_frame_size(self):
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(NOISE)
            try:
                sizes = {}
                for q in ("low", "high"):
                    async with Viewer("t1") as v:
                        await v.view(tab=tid, q=q)
                        self.assertTrue(await until(lambda: v.count() > 8, 12), f"no frames at {q}")
                        fr = v.frames[3:]
                        sizes[q] = (sum(f["n"] for f in fr) / len(fr), fr[-1]["w"])
                self.assertGreater(sizes["high"][0], sizes["low"][0] * 1.5, f"high must cost clearly more than low: {sizes}")
            finally:
                await b.close_tab(tid)

    async def test_hub_is_released_when_no_window_is_open(self):
        async with Viewer("t1") as v:
            await until(lambda: v.tabs, 5)
            self.assertTrue(len(await stats()) >= 1)
        for _ in range(60):
            if not await stats():
                break
            await asyncio.sleep(0.2)
        self.assertEqual(await stats(), [], "CDP connection must be released after the last window closes")


    # ---------------------------------------------------------------- review regressions
    async def test_input_dropped_right_after_unrequested_tab_switch(self):
        """The agent opens a tab while the human is typing: those keys were meant for the previous page."""
        async with Cdp(self.inst["cdp"]) as b:
            a = await b.new_tab(FORM)
            try:
                async with Viewer("t1") as v:
                    self.assertTrue(await until(lambda: v.active == a and v.count(v.idx_of(a)) > 2, 8))
                    await asyncio.sleep(1.2)
                    c = await b.new_tab(FORM)
                    try:
                        self.assertTrue(await until(lambda: v.active == c, 6), "view must follow the new tab")
                        for text in ("early",):
                            await v.send(t="mouse", tab=c, type="mousePressed", x=100, y=58, button="left", clickCount=1)
                            await v.send(t="mouse", tab=c, type="mouseReleased", x=100, y=58, button="left", clickCount=1)
                            await v.send(t="text", tab=c, text=text)
                        await asyncio.sleep(0.3)
                        self.assertEqual(await b.evaluate(c, "document.getElementById('f').value"), "", "input right after the switch must be dropped")
                        await asyncio.sleep(1.0)
                        await v.send(t="mouse", tab=c, type="mousePressed", x=100, y=58, button="left", clickCount=1)
                        await v.send(t="mouse", tab=c, type="mouseReleased", x=100, y=58, button="left", clickCount=1)
                        await v.send(t="text", tab=c, text="late")
                        await asyncio.sleep(0.4)
                        self.assertEqual(await b.evaluate(c, "document.getElementById('f').value"), "late", "input after the quiet period must work")
                    finally:
                        await b.close_tab(c)
            finally:
                await b.close_tab(a)

    async def test_malformed_messages_are_ignored(self):
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(ANIM)
            try:
                async with Viewer("t1") as v:
                    await v.view(tab=tid, q="low")
                    self.assertTrue(await until(lambda: v.count() > 3, 8))
                    for raw in ('[1]', '"x"', 'null', 'not json', '{"t":"mouse","type":"mouseMoved","x":1e999,"y":1}',
                                '{"t":"ack","f":' + "9" * 400 + "}", '{"t":"view","mode":5,"tab":[1],"q":{}}',
                                '{"t":"mouse","tab":"x","type":"mousePressed","buttons":"zz","mod":[]}'):
                        await v.ws.send_str(raw)
                    n = len(v.frames)
                    await asyncio.sleep(1.0)
                    self.assertFalse(v.ws.closed, "malformed messages must not close the connection")
                    self.assertGreater(len(v.frames), n, "stream must keep flowing")
            finally:
                await b.close_tab(tid)

    async def test_stream_recovers_after_a_client_stops_acking(self):
        async with Cdp(self.inst["cdp"]) as b:
            tid = await b.new_tab(NOISE)
            try:
                async with Viewer("t1") as v:
                    await v.view(tab=tid, q="low")
                    self.assertTrue(await until(lambda: v.count() > 3, 10))
                    v.ack = False
                    await asyncio.sleep(2.5)
                    v.ack = True
                    await asyncio.sleep(3.6)            # the frames that were never acked expire (3 s)
                    t0, n0 = time.monotonic(), v.count()
                    await asyncio.sleep(2.0)
                    rate = (v.count() - n0) / (time.monotonic() - t0)
                    self.assertGreater(rate, 8, f"stream did not recover after the stall ({rate:.1f} fps)")
            finally:
                await b.close_tab(tid)


def await_exit(proc, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


class CoreTests(unittest.TestCase):
    def test_names_and_loopback_only(self):
        for bad in ("", "A", "../x", "a b", "x" * 40, "-a"):
            with self.assertRaises(bv_core.ViewerError):
                bv_core.check_name(bad)
        for bad in ("http://example.com:9222", "http://10.0.0.5:9222", "https://127.0.0.1:9222", "http://127.0.0.1:9222/x", "file:///etc/passwd"):
            with self.assertRaises(bv_core.ViewerError):
                bv_core._loopback_url(bad)
        self.assertEqual(bv_core._loopback_url("http://127.0.0.1:9222/"), "http://127.0.0.1:9222")

    def test_browser_choice_never_downgrades_a_profile(self):
        with tempfile.TemporaryDirectory() as d:
            prof = Path(d)
            (prof / "Last Version").write_text("9999.0.0.0")      # written by a Chrome newer than any installed
            with self.assertRaises(bv_core.ViewerError):
                bv_core.chromium_path(prof)
            (prof / "Last Version").write_text("100.0.0.0")
            self.assertGreaterEqual(bv_core._major(bv_core.chromium_path(prof)), 100)
            self.assertEqual(bv_core.chromium_path(prof / "new"), bv_core._chromium_candidates()[0])

    def test_stale_pid_is_never_signalled(self):
        """A registry entry whose pid now belongs to another process must not get that process killed."""
        prof = bv_core.home() / "profiles" / "stale"
        prof.mkdir(parents=True, exist_ok=True)
        victim = subprocess.Popen(["sleep", "300"], start_new_session=True)       # a session leader, like a shell
        look_alike = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", f"--user-data-dir={prof}"],
                                      start_new_session=True)
        try:
            def entry(pid, starttime):
                reg = bv_core.load_instances()
                reg["stale"] = {"cdp": "http://127.0.0.1:9", "port": 9, "managed": True, "pid": pid, "starttime": starttime,
                                "profile": str(prof), "created": 0}
                bv_core._save_instances(reg)

            entry(victim.pid, None)                                    # unrelated process
            r = bv_core.stop_instance("stale", purge=True)
            self.assertFalse(r["stopped"])
            self.assertIn("note", r)
            time.sleep(0.3)
            self.assertIsNone(victim.poll(), "an unrelated process must survive")

            prof.mkdir(parents=True, exist_ok=True)
            entry(look_alike.pid, 123)                                 # right command line, wrong start time: recycled pid
            self.assertFalse(bv_core.stop_instance("stale")["stopped"])
            time.sleep(0.3)
            self.assertIsNone(look_alike.poll(), "a recycled pid must survive")

            entry(look_alike.pid, bv_core._proc_start(look_alike.pid))  # the real thing
            self.assertTrue(bv_core.stop_instance("stale")["stopped"])
            self.assertTrue(await_exit(look_alike), "our own browser must be stopped")
        finally:
            for pr in (victim, look_alike):
                pr.kill()
                pr.wait()

    def test_adopt_refuses_a_live_managed_name(self):
        a = bv_core.start_instance("own")
        try:
            with self.assertRaises(bv_core.ViewerError):
                bv_core.adopt_instance("own", a["cdp"])
            self.assertTrue(bv_core.load_instances()["own"]["managed"])
        finally:
            bv_core.stop_instance("own", purge=True)

    def test_failed_start_leaves_no_orphan(self):
        fake = Path(TMP) / "fake-chromium"
        fake.write_text('#!/bin/sh\nexec python3 -c "import time; time.sleep(300)" "$@"\n')
        fake.chmod(0o755)
        old_env, old_timeout = os.environ.get("BV_CHROMIUM"), bv_core.START_TIMEOUT
        os.environ["BV_CHROMIUM"], bv_core.START_TIMEOUT = str(fake), 1.0
        prof = bv_core.home() / "profiles" / "orphan"
        try:
            with self.assertRaises(bv_core.ViewerError):
                bv_core.start_instance("orphan")
            time.sleep(0.5)
            self.assertEqual(procs_with(f"--user-data-dir={prof}"), [], "a browser that never answered must not be left running")
            self.assertNotIn("orphan", bv_core.load_instances())
        finally:
            if old_env is None:
                os.environ.pop("BV_CHROMIUM", None)
            else:
                os.environ["BV_CHROMIUM"] = old_env
            bv_core.START_TIMEOUT = old_timeout
            shutil.rmtree(prof, ignore_errors=True)

    def test_hosts_in_any_spelling_of_wildcard_are_refused(self):
        for h in ("0.0.0.0", "0.0.0.00", "00.0.0.0", "::", "", "localhost"):
            with self.assertRaises(bv_core.ViewerError, msg=h):
                bv_core.start_server(port=9398, hosts=[h])

    def test_names_reject_trailing_newline(self):
        with self.assertRaises(bv_core.ViewerError):
            bv_core.check_name("abc\n")

    def test_wildcard_host_refused(self):
        with self.assertRaises(bv_core.ViewerError):
            bv_core.start_server(port=9399, hosts=["0.0.0.0"])

    def test_adopt_unreachable_and_unknown_stop(self):
        with self.assertRaises(bv_core.ViewerError):
            bv_core.adopt_instance("ghost", "http://127.0.0.1:9")
        with self.assertRaises(bv_core.ViewerError):
            bv_core.stop_instance("nope")

    def test_start_is_idempotent_and_stop_kills_browser(self):
        a = bv_core.start_instance("idem")
        try:
            b = bv_core.start_instance("idem")
            self.assertTrue(b["reused"])
            self.assertEqual(a["pid"], b["pid"])
            r = bv_core.stop_instance("idem", purge=True)
            self.assertTrue(r["stopped"])
            self.assertTrue(r["profile_deleted"])
            self.assertFalse(bv_core.cdp_alive(a["cdp"]))
        finally:
            try:
                bv_core.stop_instance("idem", purge=True)
            except bv_core.ViewerError:
                pass

    def test_adopt_external_is_never_killed(self):
        a = bv_core.start_instance("ext-src")
        try:
            bv_core.adopt_instance("ext", a["cdp"])
            r = bv_core.stop_instance("ext")
            self.assertFalse(r["managed"])
            self.assertTrue(bv_core.cdp_alive(a["cdp"]), "stopping an adopted browser must leave it running")
        finally:
            bv_core.stop_instance("ext-src", purge=True)

    def test_bootstrap_tokens_are_single_use_expire_and_store_only_a_hash(self):
        tok = bv_core.new_bootstrap()
        d = bv_core.home() / "bootstrap"
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        names = [f.name for f in d.iterdir()]
        self.assertTrue(names and all(tok not in n for n in names), "the token itself must not be on disk")
        self.assertTrue(all(oct(f.stat().st_mode & 0o777) == "0o600" for f in d.iterdir()))
        self.assertTrue(bv_core.consume_bootstrap(tok))
        self.assertFalse(bv_core.consume_bootstrap(tok))
        self.assertFalse(bv_core.consume_bootstrap(bv_core.new_bootstrap(ttl=-1)))
        for bad in ("", None, 5, "x" * 300, "../../etc/passwd"):
            self.assertFalse(bv_core.consume_bootstrap(bad))
        a, b = bv_core.new_bootstrap(), bv_core.new_bootstrap()
        bv_core.purge_bootstrap()
        self.assertFalse(bv_core.consume_bootstrap(a) or bv_core.consume_bootstrap(b))

    def test_links_carry_a_fresh_one_time_token_each(self):
        real = bv_core.server_info
        bv_core.server_info = lambda: {"port": 1234, "hosts": ["127.0.0.1", "10.1.2.3"]}
        try:
            links = bv_core.links("x")
        finally:
            bv_core.server_info = real
        self.assertEqual(len(links), 2)
        tokens = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["b"][0] for u in links]
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertTrue(all("&i=x" in u and bv_core.get_token() not in u for u in links))
        for tk in tokens:
            self.assertTrue(bv_core.consume_bootstrap(tk))

    def test_token_is_private_and_stable(self):
        t1 = bv_core.get_token()
        self.assertEqual(t1, bv_core.get_token())
        self.assertEqual(oct((bv_core.home() / "token").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(bv_core.home().stat().st_mode & 0o777), "0o700")


# --------------------------------------------------------------------------- HTTPS
def _self_signed(directory: Path):
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "2", "-subj", "/CN=box.test.ts.net",
                    "-addext", "subjectAltName=DNS:box.test.ts.net,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert, key


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a throw-away certificate")
class TlsTests(unittest.IsolatedAsyncioTestCase):
    TLS_PORT = 9398

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="bv-tls-"))
        cls.addClassCleanup(shutil.rmtree, cls.dir, True)
        cls.cert, cls.key = _self_signed(cls.dir)
        (cls.dir / "home").mkdir()
        cls.env = dict(os.environ, BROWSER_VIEWER_HOME=str(cls.dir / "home"))
        cls.proc = subprocess.Popen(
            [sys.executable, str(Path(bv_core.__file__).with_name("bv_server.py")), "--host", "127.0.0.1",
             "--tls-host", "127.0.0.1", "--cert", str(cls.cert), "--key", str(cls.key), "--tls-name", "box.test.ts.net",
             "--port", str(cls.TLS_PORT), "--idle", "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=cls.env)
        cls.addClassCleanup(lambda: (cls.proc.terminate(), cls.proc.wait(10)))
        cls.ctx = ssl.create_default_context(cafile=str(cls.cert))

    def _bootstrap(self):
        old = os.environ["BROWSER_VIEWER_HOME"]
        os.environ["BROWSER_VIEWER_HOME"] = self.env["BROWSER_VIEWER_HOME"]
        try:
            return bv_core.new_bootstrap()
        finally:
            os.environ["BROWSER_VIEWER_HOME"] = old

    async def test_https_login_cookie_is_secure_and_wss_works(self):
        base = f"https://127.0.0.1:{self.TLS_PORT}"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            self.assertTrue(await until(lambda: self.proc.poll() is None, 1))
            for _ in range(100):                                   # the server needs a moment to start
                try:
                    if (await s.get(f"{base}/healthz", ssl=self.ctx)).status == 200:
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
            else:
                self.fail("the HTTPS server did not come up")
            with self.assertRaises(aiohttp.ClientError):          # the same port does not answer plain HTTP
                await s.get(f"http://127.0.0.1:{self.TLS_PORT}/healthz")
            r = await s.post(f"{base}/api/session", json={"b": self._bootstrap()}, headers={"Origin": base}, ssl=self.ctx)
            self.assertEqual(r.status, 200)
            cookie = r.headers.getone("Set-Cookie")
            for flag in ("Secure", "HttpOnly", "SameSite=Strict"):
                self.assertIn(flag, cookie)
            self.assertEqual((await s.get(f"{base}/api/auth", ssl=self.ctx)).status, 200)
            ws = await s.ws_connect(f"wss://127.0.0.1:{self.TLS_PORT}/ws", ssl=self.ctx)
            self.assertEqual((await ws.receive(timeout=5)).type, aiohttp.WSMsgType.TEXT)
            await ws.close()
            with self.assertRaises(aiohttp.WSServerHandshakeError):   # an Origin that is not this server is refused
                await s.ws_connect(f"wss://127.0.0.1:{self.TLS_PORT}/ws", ssl=self.ctx, headers={"Origin": "https://evil.example"})

    def test_server_refuses_inconsistent_tls_arguments(self):
        base = [sys.executable, str(Path(bv_core.__file__).with_name("bv_server.py")), "--port", "9399", "--idle", "0"]
        for extra in (["--host", "127.0.0.1", "--tls-host", "127.0.0.1"],                       # no certificate
                      ["--host", "127.0.0.1", "--tls-host", "10.9.9.9", "--cert", "c", "--key", "k", "--tls-name", "x"]):  # not a --host
            r = subprocess.run(base + extra, capture_output=True, text=True, env=self.env, timeout=20)
            self.assertNotEqual(r.returncode, 0, extra)


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a throw-away certificate")
class TlsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bv-tlsset-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old_home = os.environ["BROWSER_VIEWER_HOME"]
        os.environ["BROWSER_VIEWER_HOME"] = str(self.d / "home")
        self.addCleanup(os.environ.__setitem__, "BROWSER_VIEWER_HOME", self.old_home)
        self.cert, self.key = _self_signed(self.d)

    def test_links_use_the_certificate_name_only_for_tls_hosts(self):
        real = bv_core.server_info
        bv_core.server_info = lambda: {"port": 1234, "hosts": ["127.0.0.1", "10.1.2.3", "10.1.2.4"],
                                       "tls": {"hosts": ["10.1.2.3", "10.1.2.4"], "name": "box.example.ts.net"}}
        try:
            links = bv_core.links("x")
        finally:
            bv_core.server_info = real
        self.assertEqual([u.split("/?b=")[0] for u in links], ["http://127.0.0.1:1234", "https://box.example.ts.net:1234"])

    def test_files_mode_validates_and_off_removes(self):
        self.assertIsNone(bv_core.tls_settings())
        good = dict(cert=str(self.cert), key=str(self.key))
        for label, kwargs in (("no name", dict(good)),
                              ("bad name", dict(good, name="bad name!")),
                              ("name with a slash", dict(good, name="a/b.example")),
                              ("missing key file", dict(cert=str(self.cert), key=str(self.d / "missing.pem"), name="ok.example")),
                              ("no certificate", dict(key=str(self.key), name="ok.example"))):
            with self.assertRaises(bv_core.ViewerError, msg=label):
                bv_core.set_tls("files", **kwargs)
            self.assertIsNone(bv_core.tls_settings(), label)
        res = bv_core.set_tls("files", name="Box.Test.ts.net", **good)
        self.assertEqual(res["name"], "box.test.ts.net")
        self.assertEqual(bv_core.tls_settings()["name"], "box.test.ts.net")
        self.assertEqual(oct((bv_core.home() / "tls.json").stat().st_mode & 0o777), "0o600")
        with self.assertRaises(bv_core.ViewerError):
            bv_core.set_tls("nonsense")
        bv_core.set_tls("off")
        self.assertIsNone(bv_core.tls_settings())
        self.assertTrue(self.key.exists(), "a key you supplied is yours: off must never delete it")

    def _fake_tailscale(self, cert_domains):
        bin_dir = self.d / "bin"
        bin_dir.mkdir()
        script = bin_dir / "tailscale"
        script.write_text(f"""#!/usr/bin/env python3
import json, shutil, sys
a = sys.argv[1:]
if a[:2] == ["status", "--json"]:
    print(json.dumps({{"Self": {{"DNSName": "box.test.ts.net."}}, "CertDomains": {cert_domains!r}}}))
elif a[0] == "cert":
    cert, key = a[a.index("--cert-file") + 1], a[a.index("--key-file") + 1]
    open({str(self.d / "tailscale-calls.log")!r}, "a").write(" ".join(a) + "\\n")
    shutil.copy({str(self.cert)!r}, cert); shutil.copy({str(self.key)!r}, key)
""")
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{old}"
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def test_tailscale_mode_asks_tailscale_for_this_machines_certificate(self):
        self._fake_tailscale(["box.test.ts.net"])
        res = bv_core.set_tls("tailscale")
        self.assertEqual(res["name"], "box.test.ts.net")
        rec = bv_core.tls_settings()
        self.assertEqual((rec["mode"], rec["name"]), ("tailscale", "box.test.ts.net"))
        self.assertTrue(Path(rec["cert"]).is_file() and Path(rec["key"]).is_file())
        self.assertEqual(oct(Path(rec["key"]).stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((bv_core.home() / "tls").stat().st_mode & 0o777), "0o700")
        calls = (self.d / "tailscale-calls.log").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        args = calls[0].split()
        self.assertEqual(args[0], "cert")
        self.assertEqual(args[args.index("--cert-file") + 1], rec["cert"])
        self.assertEqual(args[args.index("--key-file") + 1], rec["key"])
        self.assertEqual(args[-1], "box.test.ts.net", "tailscale cert must be asked for this machine's own name")
        bv_core.set_tls("off")
        self.assertFalse(Path(rec["key"]).exists(), "off removes the key copy that tailscale mode made")

    @staticmethod
    def _local_ipv4():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()
        return None if ip.startswith("127.") else ip

    def test_start_server_serves_https_on_the_non_loopback_address_and_links_use_the_name(self):
        ip = self._local_ipv4()
        if not ip:
            self.skipTest("no non-loopback IPv4 address on this machine")
        bv_core.set_tls("files", cert=str(self.cert), key=str(self.key), name="box.test.ts.net")
        info = bv_core.start_server(port=9396, hosts=["127.0.0.1", ip], idle_minutes=0)
        self.addCleanup(bv_core.stop_server)
        self.assertEqual(info["tls"]["hosts"], [ip])
        self.assertEqual(info["tls"]["name"], "box.test.ts.net")
        self.assertIsNotNone(bv_core.server_info(), "the liveness probe must work with a TLS address")
        self.assertEqual([u.split("/?b=")[0] for u in bv_core.links()], ["http://127.0.0.1:9396", "https://box.test.ts.net:9396"])
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(f"https://{ip}:9396/healthz", timeout=5, context=ctx) as r:
            self.assertEqual(r.status, 200)
        with self.assertRaises(Exception):
            urllib.request.urlopen(f"http://{ip}:9396/healthz", timeout=5)       # plain HTTP is not served there
        with urllib.request.urlopen("http://127.0.0.1:9396/healthz", timeout=5) as r:     # loopback stays plain
            self.assertEqual(r.status, 200)

    def test_tailscale_trouble_never_blocks_start_and_never_downgrades(self):
        ip = self._local_ipv4()
        if not ip:
            self.skipTest("no non-loopback IPv4 address on this machine")
        self._fake_tailscale(["box.test.ts.net"])
        bv_core.set_tls("tailscale")
        calls = self.d / "tailscale-calls.log"
        calls.write_text("")
        broken = self.d / "bin" / "tailscale"
        broken.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
        # loopback only: TLS is not in play, tailscale must not even be asked
        info = bv_core.start_server(port=9395, hosts=["127.0.0.1"], idle_minutes=0)
        self.assertNotIn("tls", info)
        bv_core.stop_server()
        # a TLS address while tailscale is down: the certificate on disk is reused, with a warning, still over HTTPS
        info = bv_core.start_server(port=9395, hosts=["127.0.0.1", ip], idle_minutes=0)
        self.addCleanup(bv_core.stop_server)
        self.assertEqual(info["tls"]["hosts"], [ip])
        self.assertTrue(info.get("warnings") and "could not refresh" in info["warnings"][0], info)

    def test_a_server_that_dies_at_start_is_reported_at_once_with_the_reason(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        t0 = time.time()
        with self.assertRaises(bv_core.ViewerError) as cm:
            bv_core.start_server(port=busy.getsockname()[1], hosts=["127.0.0.1"], idle_minutes=0)
        self.assertLess(time.time() - t0, 25, "must not wait out the 60 s timeout")
        self.assertIn("exited", str(cm.exception))

    def test_tailscale_mode_refuses_when_the_tailnet_offers_no_certificate(self):
        self._fake_tailscale([])
        with self.assertRaises(bv_core.ViewerError) as cm:
            bv_core.set_tls("tailscale")
        self.assertIn("HTTPS", str(cm.exception))
        self.assertIsNone(bv_core.tls_settings())


# --------------------------------------------------------------------------- the tool layer and the registry
class _FakeCdp(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x", "Browser": "Fake/1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PluginActionTests(unittest.TestCase):
    """__init__.py (the tool) against a fake `hermes` on PATH and a fake CDP endpoint."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bv-act-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old_home = os.environ["BROWSER_VIEWER_HOME"]
        os.environ["BROWSER_VIEWER_HOME"] = str(self.d / "home")
        self.addCleanup(os.environ.__setitem__, "BROWSER_VIEWER_HOME", self.old_home)
        self.log = self.d / "hermes.log"
        self.fail_flag = self.d / "fail-unset"
        bindir = self.d / "bin"
        bindir.mkdir()
        fake = bindir / "hermes"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {self.log}\n"
                        f"if [ \"$2\" = unset ] && [ -e {self.fail_flag} ]; then echo boom >&2; exit 1; fi\nexit 0\n")
        fake.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}:{old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeCdp)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.cdp = f"http://127.0.0.1:{self.srv.server_address[1]}"
        spec = importlib.util.spec_from_file_location("bvplugin", Path(bv_core.__file__).with_name("__init__.py"),
                                                      submodule_search_locations=[str(Path(bv_core.__file__).parent)])
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules["bvplugin"] = self.plugin
        self.addCleanup(sys.modules.pop, "bvplugin", None)
        spec.loader.exec_module(self.plugin)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_use_sets_and_stop_clears_browser_cdp_url(self):
        run = self.plugin.run_action
        run({"action": "adopt", "name": "fx", "cdp_url": self.cdp})
        run({"action": "use", "name": "fx"})
        self.assertEqual(self.calls()[-1], f"config set browser.cdp_url {self.cdp}")
        res = run({"action": "stop", "name": "fx"})
        self.assertEqual(self.calls()[-1], "config unset browser.cdp_url")
        self.assertIn("browser_exec", res)
        self.assertNotIn("fx", self.plugin.bv_core.load_instances())

    def test_if_clearing_browser_cdp_url_fails_the_browser_is_not_stopped(self):
        run = self.plugin.run_action
        run({"action": "adopt", "name": "fx", "cdp_url": self.cdp})
        run({"action": "use", "name": "fx"})
        self.fail_flag.write_text("")
        with self.assertRaises(self.plugin.bv_core.ViewerError):
            run({"action": "stop", "name": "fx"})
        self.assertIn("fx", self.plugin.bv_core.load_instances(), "nothing may be stopped while browser_exec still points at it")
        self.fail_flag.unlink()
        run({"action": "stop", "name": "fx"})
        self.assertNotIn("fx", self.plugin.bv_core.load_instances())

    def test_names_are_validated_before_they_reach_a_link(self):
        for bad in ("x&evil=1", "x#frag", "../x", "a b", "X"):
            with self.assertRaises(self.plugin.bv_core.ViewerError, msg=bad):
                self.plugin.run_action({"action": "link", "name": bad})
            with self.assertRaises(self.plugin.bv_core.ViewerError, msg=bad):
                self.plugin.run_action({"action": "rotate_token", "name": bad})

    def test_status_never_returns_a_tab_path_or_query(self):
        self.assertEqual(bv_core._origin("https://accounts.example.com/o/oauth2/auth?code=SECRET&state=x#frag"), "https://accounts.example.com")
        self.assertEqual(bv_core._origin("http://127.0.0.1:9222/path?q=1"), "http://127.0.0.1:9222")
        self.assertEqual(bv_core._origin("data:text/html,<h1>secret</h1>"), "data:")
        self.assertEqual(bv_core._origin("about:blank"), "about:")
        self.assertEqual(bv_core._origin("not a url"), "")

    def test_purge_is_confined_to_the_profiles_directory(self):
        outside = self.d / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        core = self.plugin.bv_core
        core._save_instances({"fx": {"cdp": self.cdp, "port": int(self.cdp.rsplit(":", 1)[1]), "managed": False,
                                     "pid": None, "profile": str(outside), "created": 0}})
        core.stop_instance("fx", purge=True)
        self.assertTrue((outside / "keep.txt").exists(), "purge must never delete a directory outside profiles/")
        inside = core.home() / "profiles"
        inside.mkdir(parents=True, exist_ok=True)
        (inside / "gone").mkdir()
        core._save_instances({"gy": {"cdp": self.cdp, "port": 1, "managed": False, "pid": None, "profile": str(inside / "gone"), "created": 0}})
        core.stop_instance("gy", purge=True)
        self.assertFalse((inside / "gone").exists())

    def test_the_child_environment_carries_no_secrets(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        env = bv_core._clean_env({"BROWSER_VIEWER_HOME": "/x"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertIn("PATH", env)
        self.assertEqual(env["BROWSER_VIEWER_HOME"], "/x")


if __name__ == "__main__":
    unittest.main()
