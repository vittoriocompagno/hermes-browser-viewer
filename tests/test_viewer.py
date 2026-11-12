"""Integration tests for the browser-viewer plugin: real Chromium, real server, real WebSocket clients.

Run (from the plugin directory):
    uv run --with aiohttp python -m unittest -v tests.test_viewer

Everything runs on scratch ports with a throw-away data dir: a browser or viewer you already use
is never touched.
"""
import asyncio
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="bv-test-")
os.environ["BROWSER_VIEWER_HOME"] = TMP
os.environ["BV_CLOSE_GRACE"] = "1"
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
        self.ws = await self.s.ws_connect(f"{BASE}/ws?k={self.token}&i={self.instance}", max_msg_size=2 ** 24)
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


async def stats():
    async with aiohttp.ClientSession() as s:
        return await (await s.get(f"{BASE}/api/stats?k={bv_core.get_token()}")).json()


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
        async with aiohttp.ClientSession() as s:
            tok = bv_core.get_token()
            r = await s.get(f"{BASE}/?k={tok}")
            self.assertEqual(r.status, 200)
            self.assertIn("no-store", r.headers.get("Cache-Control", ""))
            self.assertNotIn(tok, await r.text(), "the page itself must never contain the token")
            self.assertEqual((await s.get(f"{BASE}/")).status, 200)      # static shell: a reload works without ?k=
            self.assertEqual((await s.get(f"{BASE}/api/stats?k=wrong")).status, 403)
            self.assertEqual((await s.get(f"{BASE}/api/stats")).status, 403)
            self.assertEqual((await s.get(f"{BASE}/healthz")).status, 200)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await s.ws_connect(f"{BASE}/ws?k=wrong&i=t1")
            self.assertEqual(cm.exception.status, 403)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:   # cross-site page with a valid token
                await s.ws_connect(f"{BASE}/ws?k={bv_core.get_token()}&i=t1", headers={"Origin": "http://evil.example"})
            self.assertEqual(cm.exception.status, 403)

    async def test_token_rotation_without_restart(self):
        old = bv_core.get_token()
        new = bv_core.get_token(rotate=True)
        async with aiohttp.ClientSession() as s:
            self.assertEqual((await s.get(f"{BASE}/api/stats?k={old}")).status, 403)
            self.assertEqual((await s.get(f"{BASE}/api/stats?k={new}")).status, 200)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await s.ws_connect(f"{BASE}/ws?k={old}&i=t1")
            self.assertEqual(cm.exception.status, 403)

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

    async def test_malformed_messages_are_ignored_and_non_ascii_token_is_403(self):
        async with aiohttp.ClientSession() as s:
            self.assertEqual((await s.get(f"{BASE}/api/stats?k=%C3%A9")).status, 403)    # non-ASCII token: 403, never a 500
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
            out = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={prof}"], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "", "a browser that never answered must not be left running")
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


if __name__ == "__main__":
    unittest.main()
