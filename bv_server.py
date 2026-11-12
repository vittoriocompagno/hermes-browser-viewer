#!/usr/bin/env python3
"""browser-viewer server: streams Chromium tabs (CDP) to a web page and relays mouse/keyboard.

Design
- One Hub per browser instance (one CDP connection), many viewer windows (Clients) per Hub.
- A tab is streamed once, however many windows watch it (screencast fan-out).
- Frames travel as binary WebSocket messages (no base64): 9-byte header + JPEG.
- Per client: only the newest frame is kept, a small send window is enforced and the browser is
  throttled (screencast ack is held) while every watcher is busy, so latency does not build up.
- Quality adapts to the client's measured round-trip time; the user can also pin a level.
- Mosaic mode shows every tab with low-cost snapshots (hidden tabs emit no screencast frames).
- Security: a signed session cookie (or the master token) on every request, Origin must match Host, loopback-only CDP endpoints,
  bind only to explicit addresses, never logs keystrokes, pasted text or the token.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import signal
import ssl
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bv_core  # noqa: E402

INTERNAL = ("chrome://", "devtools://", "chrome-extension://", "chrome-untrusted://")
# level -> (jpeg quality, max width, max height)
LEVELS = {"thumb": (35, 400, 300), "low": (40, 640, 480), "med": (55, 960, 720), "high": (70, 1280, 1000)}
ORDER = ["thumb", "low", "med", "high"]
CAST_LEVELS = ("low", "med", "high")
HOLD_TIMEOUT = 0.5      # never keep the browser waiting longer than this for a slow viewer
def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """A numeric setting from the environment: anything unreadable, infinite or out of range falls back or is clamped."""
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        return default
    return default if v != v else min(max(v, lo), hi)          # NaN falls back, inf is clamped


CLOSE_GRACE = _env_float("BV_CLOSE_GRACE", 20.0, 0.0, 3600.0)   # seconds an empty hub keeps its CDP connection
SNAP_INTERVAL = 2.0     # mosaic snapshot refresh
MOSAIC_MAX = 12
MAX_CLIENTS_PER_HUB = 16
ACTIVE_DWELL = 1.0      # follow mode does not switch tabs more often than this
INPUT_QUIET = 0.8       # input is dropped this long after the tab a window shows changed by itself
HDR = struct.Struct(">BHHHH")   # kind, tab idx, css width, css height, frame id


def num(v: Any, d: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e7 else d
    except (TypeError, ValueError, OverflowError):
        return d


def is_page(info: Dict[str, Any]) -> bool:
    return info.get("type") == "page" and not str(info.get("url", "")).startswith(INTERNAL)


class Stream:
    """One tab being delivered to at least one client."""

    def __init__(self, hub: "Hub", tid: str):
        self.hub, self.tid = hub, tid
        self.sid: Optional[str] = None      # CDP session id of the tab
        self.level: Optional[str] = None
        self.holds: list = []               # screencast acks we owe Chromium, in arrival order
        self.hold_timer: Optional[asyncio.TimerHandle] = None
        self.frames = 0
        self.bytes = 0
        self.last: Optional[tuple] = None   # newest frame (w, h, jpeg): a window that joins later must not wait for the page to change

    @property
    def mode(self) -> str:
        return "poll" if self.level == "thumb" else "cast"


class Client:
    WINDOW = 2

    def __init__(self, ws: web.WebSocketResponse, hub: "Hub"):
        self.ws, self.hub = ws, hub
        self.view, self.pin, self.q = "single", None, "auto"
        self.auto_level = "med"
        self.pending: Dict[str, tuple] = {}     # tab id -> (w, h, jpeg): newest frame only
        self.inflight: Dict[int, float] = {}    # frame id -> monotonic send time
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.fid = 0
        self.rtt: Optional[float] = None
        self.drops = self.sent = self.bytes = 0
        self._drops_window = 0
        self._adapt_at = time.monotonic()
        self._good_since = time.monotonic()
        self.opened = time.time()
        self._main: Optional[str] = None
        self._main_at = 0.0

    def note_main(self, involuntary: bool = True) -> None:
        """Track the tab this window shows. A change the user did not ask for (agent opened a tab) opens a quiet period."""
        m = self.main_tid() if self.view == "single" else None
        if m != self._main:
            self._main = m
            if involuntary:
                self._main_at = time.monotonic()

    # -- what this client wants to see
    def level(self) -> str:
        return self.q if self.q in ORDER else self.auto_level

    def main_tid(self) -> Optional[str]:
        return self.pin if self.pin in self.hub.tabs else self.hub.active

    def wanted(self) -> Dict[str, str]:
        if self.view == "mosaic":
            tids = [t for t in reversed(self.hub.order) if t in self.hub.tabs][:MOSAIC_MAX]
            return {t: "thumb" for t in tids}
        tid = self.main_tid()
        lvl = self.level()
        return {tid: lvl if lvl in CAST_LEVELS else "low"} if tid else {}

    # -- transport (all sends of a client are serialised)
    async def send_json(self, obj: Dict[str, Any]) -> None:
        async with self.send_lock:
            if not self.ws.closed:
                await self.ws.send_str(json.dumps(obj))

    def offer(self, tid: str, frame: tuple) -> None:
        if tid in self.pending:
            self.drops += 1
            self._drops_window += 1
        self.pending[tid] = frame
        self.wake.set()

    def has_capacity(self) -> bool:
        return len(self.inflight) < self.WINDOW and not self.pending

    async def sender(self) -> None:
        try:
            await self._send_loop()
        except Exception as exc:  # noqa: BLE001
            print(f"viewer: sender stopped ({type(exc).__name__})", flush=True)
            with contextlib.suppress(Exception):
                await self.ws.close()

    async def _send_loop(self) -> None:
        while not self.ws.closed:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 1.0)
            self.wake.clear()
            now = time.monotonic()
            lost = [f for f, t in self.inflight.items() if now - t > 3.0]
            for fid in lost:
                del self.inflight[fid]          # a lost ack must not stall the stream
            if lost:
                self.rtt = max(self.rtt or 0.0, 3.0)
                self._adapt()
            while self.pending and len(self.inflight) < self.WINDOW and not self.ws.closed:
                tid = next(iter(self.pending))
                w, h, jpg = self.pending.pop(tid)
                tab = self.hub.tabs.get(tid)
                if not tab:
                    continue
                self.fid = (self.fid + 1) & 0xFFFF
                self.inflight[self.fid] = time.monotonic()
                async with self.send_lock:
                    await self.ws.send_bytes(HDR.pack(1, tab["idx"], w, h, self.fid) + jpg)
                self.sent += 1
                self.bytes += len(jpg)
                self.hub.release_holds()

    def on_ack(self, fid: int) -> None:
        t = self.inflight.pop(fid, None)
        if t is None:
            return
        dt = time.monotonic() - t
        self.rtt = dt if self.rtt is None else 0.8 * self.rtt + 0.2 * dt
        self.wake.set()
        self.hub.release_holds()
        self._adapt()

    def _adapt(self) -> None:
        now = time.monotonic()
        if self.q in ORDER or now - self._adapt_at < 2.0 or self.rtt is None:
            return
        i = ORDER.index(self.auto_level)
        if (self.rtt > 0.20 or self._drops_window > 15) and i > 1:
            self.auto_level = ORDER[i - 1]
            self._good_since = now
        elif self.rtt < 0.07 and self._drops_window == 0 and now - self._good_since > 6.0 and i < 3:
            self.auto_level = ORDER[i + 1]
            self._good_since = now
        elif self.rtt > 0.07 or self._drops_window:
            self._good_since = now
        self._adapt_at, self._drops_window = now, 0
        self.hub.dirty.set()


class Hub:
    """One browser instance: one CDP connection shared by every viewer window."""

    def __init__(self, name: str, cdp: str):
        self.name, self.cdp = name, cdp
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        self.br: Optional[aiohttp.ClientWebSocketResponse] = None
        self.clients: set = set()
        self.tabs: Dict[str, Dict[str, Any]] = {}   # target id -> {url, title, idx}; creation order
        self.order: list = []                        # recency, newest last
        self.active: Optional[str] = None
        self._active_at = 0.0
        self._active_timer: Optional[asyncio.TimerHandle] = None
        self._next_idx = 1
        self.streams: Dict[str, Stream] = {}
        self.sid_map: Dict[str, Stream] = {}
        self._pending: Dict[int, asyncio.Future] = {}
        self._mid = 0
        self.dirty = asyncio.Event()
        self.tabs_dirty = asyncio.Event()
        self.lock = asyncio.Lock()
        self.tasks: list = []
        self.closed = False
        self._activated: Optional[str] = None
        self._close_timer: Optional[asyncio.TimerHandle] = None

    # -- lifecycle
    async def start(self) -> None:
        async with self.http.get(self.cdp + "/json/version") as r:
            ver = await r.json()
        async with self.http.get(self.cdp + "/json/list") as r:
            listing = await r.json()
        path = urlparse(ver["webSocketDebuggerUrl"]).path
        if not path.startswith("/devtools/browser/"):
            raise RuntimeError("unexpected CDP websocket path")
        self.br = await self.http.ws_connect(f"ws://{self.cdp.split('//', 1)[1]}{path}", max_msg_size=2 ** 26)
        initial = [t for t in listing if is_page(t)]          # /json/list is newest first
        for t in reversed(initial):
            self._add_tab(t["id"], t.get("url", ""), t.get("title", ""))
        first = next((t for t in initial if str(t.get("url", "")).startswith("http")), initial[0] if initial else None)
        self.active = first["id"] if first else None
        self.tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._reconciler()),
                      asyncio.create_task(self._tabs_loop()), asyncio.create_task(self._snap_loop())]
        await self.call("Target.setDiscoverTargets", discover=True)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for t in self.tasks:
            t.cancel()
        for tid in list(self.streams):
            with contextlib.suppress(Exception):
                await self._stop_stream(tid)
        if self.br is not None:
            with contextlib.suppress(Exception):
                await self.br.close()
        await self.http.close()
        for c in list(self.clients):
            with contextlib.suppress(Exception):
                await c.send_json({"t": "err", "u": "browser disconnected"})
                await c.ws.close()

    # -- CDP plumbing
    async def call(self, method: str, sid: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        self._mid += 1
        i = self._mid
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        msg: Dict[str, Any] = {"id": i, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        try:
            await self.br.send_json(msg)
            return await asyncio.wait_for(fut, 10)
        finally:
            self._pending.pop(i, None)

    async def fire(self, method: str, sid: Optional[str], **params: Any) -> None:
        self._mid += 1
        msg: Dict[str, Any] = {"id": self._mid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        if self.br is not None and not self.br.closed:
            await self.br.send_json(msg)

    # -- tabs
    def _add_tab(self, tid: str, url: str, title: str) -> None:
        if tid not in self.tabs:
            self.tabs[tid] = {"url": url, "title": title, "idx": self._next_idx}
            self._next_idx += 1
            self.order.append(tid)

    def _set_active(self, tid: str) -> None:
        if tid not in self.tabs or tid == self.active:
            return
        if tid in self.order:
            self.order.remove(tid)
        self.order.append(tid)
        now = time.monotonic()
        wait = self._active_at + ACTIVE_DWELL - now
        if wait > 0:        # a navigation burst in two tabs must not make the view flip-flop
            if self._active_timer:
                self._active_timer.cancel()
            self._active_timer = asyncio.get_running_loop().call_later(wait, self._apply_active)
            return
        self._apply_active()

    def _apply_active(self) -> None:
        tid = self.order[-1] if self.order else None
        if tid and tid in self.tabs and tid != self.active:
            self.active = tid
            self._active_at = time.monotonic()
            for c in self.clients:
                c.note_main()
            self.dirty.set()
            self.tabs_dirty.set()

    def tabs_msg(self) -> Dict[str, Any]:
        return {"t": "tabs", "active": self.active,
                "tabs": [{"id": t, "idx": v["idx"], "url": v["url"][:300], "title": v["title"][:120]} for t, v in self.tabs.items()]}

    # -- background tasks
    async def _pump(self) -> None:
        try:
            async for msg in self.br:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                d = json.loads(msg.data)
                if "id" in d:
                    fut = self._pending.get(d["id"])
                    if fut and not fut.done():
                        if "error" in d:
                            fut.set_exception(RuntimeError(d["error"].get("message", "cdp error")))
                        else:
                            fut.set_result(d.get("result", {}))
                    continue
                await self._event(d.get("method"), d.get("params", {}), d.get("sessionId"))
        except Exception as exc:  # noqa: BLE001
            print(f"hub {self.name}: pump error {exc!r}", flush=True)
        finally:
            asyncio.create_task(self.close())

    async def _event(self, ev: Optional[str], p: Dict[str, Any], sid: Optional[str]) -> None:
        if ev == "Page.screencastFrame":
            st = self.sid_map.get(sid or "")
            if st:
                self._on_cast_frame(st, p)
        elif ev == "Target.targetCreated":
            info = p["targetInfo"]
            if is_page(info) and info["targetId"] not in self.tabs:
                self._add_tab(info["targetId"], info.get("url", ""), info.get("title", ""))
                self._activated = None
                self._set_active(info["targetId"])
                self.tabs_dirty.set()
                self.dirty.set()
        elif ev == "Target.targetInfoChanged":
            info = p["targetInfo"]
            tid = info["targetId"]
            if info.get("type") != "page" or tid not in self.tabs:
                return
            url, title = info.get("url", ""), info.get("title", "")
            if url.startswith(INTERNAL):
                return
            v = self.tabs[tid]
            url_changed = v["url"] != url
            if url_changed or v["title"] != title:
                v["url"], v["title"] = url, title
                self.tabs_dirty.set()
            if url_changed:
                self._set_active(tid)
        elif ev == "Target.targetDestroyed":
            tid = p["targetId"]
            if tid in self.tabs:
                self.tabs.pop(tid)
                if tid in self.order:
                    self.order.remove(tid)
                if tid in self.streams:
                    await self._stop_stream(tid, detach=False)
                if tid == self.active:
                    self.active = self.order[-1] if self.order else None
                    for c in self.clients:
                        c.note_main()
                self.tabs_dirty.set()
                self.dirty.set()
        elif ev == "Target.detachedFromTarget":
            st = self.sid_map.get(p.get("sessionId", ""))
            if st:
                await self._stop_stream(st.tid, detach=False)
                self.dirty.set()
        elif ev == "Target.targetCrashed":
            if p.get("targetId") in self.streams:
                await self._stop_stream(p["targetId"], detach=False)
                self.dirty.set()

    async def _tabs_loop(self) -> None:
        while True:
            await self.tabs_dirty.wait()
            await asyncio.sleep(0.15)
            self.tabs_dirty.clear()
            msg = self.tabs_msg()
            for c in list(self.clients):
                with contextlib.suppress(Exception):
                    await c.send_json(msg)

    async def _reconciler(self) -> None:
        while True:
            await self.dirty.wait()
            await asyncio.sleep(0.05)
            self.dirty.clear()
            try:
                await self.reconcile()
            except Exception as exc:  # noqa: BLE001
                print(f"hub {self.name}: reconcile error {exc!r}", flush=True)

    async def _snap_loop(self) -> None:
        while True:
            await asyncio.sleep(SNAP_INTERVAL)
            for st in list(self.streams.values()):
                if st.mode == "poll" and st.sid:
                    with contextlib.suppress(Exception):
                        await self._snap(st)

    # -- streams
    def subscribers(self, tid: str):
        return [c for c in self.clients if tid in c.wanted()]

    async def reconcile(self) -> None:
        async with self.lock:
            desired: Dict[str, str] = {}
            for c in self.clients:
                for tid, lvl in c.wanted().items():
                    if tid in self.tabs and (tid not in desired or ORDER.index(lvl) > ORDER.index(desired[tid])):
                        desired[tid] = lvl
            for tid in [t for t in self.streams if t not in desired]:
                await self._stop_stream(tid)
            for tid, lvl in desired.items():
                st = self.streams.get(tid)
                if st is None:
                    await self._start_stream(tid, lvl)
                elif st.level != lvl:
                    await self._set_level(st, lvl)
            # the tab a single-view window looks at must be the visible one, or Chromium emits no frames
            for c in self.clients:
                if c.view == "single":
                    tid = c.main_tid()
                    st = self.streams.get(tid) if tid else None
                    if st and st.mode == "cast" and self._activated != tid:
                        with contextlib.suppress(Exception):
                            await self.call("Target.activateTarget", targetId=tid)
                        self._activated = tid
                        break

    async def _start_stream(self, tid: str, lvl: str) -> None:
        st = Stream(self, tid)
        try:
            r = await self.call("Target.attachToTarget", targetId=tid, flatten=True)
            st.sid = r["sessionId"]
            self.streams[tid] = st
            self.sid_map[st.sid] = st
            st.level = lvl
            if st.mode == "cast":
                await self._begin_cast(st)
            else:
                await self._snap(st)
        except Exception as exc:  # noqa: BLE001
            print(f"hub {self.name}: start stream failed {exc!r}", flush=True)
            await self._stop_stream(tid)

    async def _begin_cast(self, st: Stream) -> None:
        q, w, h = LEVELS[st.level]
        await self.call("Page.enable", st.sid)
        await self.call("Emulation.setFocusEmulationEnabled", st.sid, enabled=True)
        await self.call("Page.startScreencast", st.sid, format="jpeg", quality=q, maxWidth=w, maxHeight=h, everyNthFrame=1)

    async def _set_level(self, st: Stream, lvl: str) -> None:
        was_cast = st.mode == "cast"
        if was_cast:
            with contextlib.suppress(Exception):
                await self.call("Page.stopScreencast", st.sid)
            self._clear_hold(st)
        st.level = lvl
        if st.mode == "cast":
            await self._begin_cast(st)
        else:
            await self._snap(st)

    async def _stop_stream(self, tid: str, detach: bool = True) -> None:
        st = self.streams.pop(tid, None)
        if not st:
            return
        self._clear_hold(st)
        if st.sid:
            self.sid_map.pop(st.sid, None)
            if detach and not self.closed:
                with contextlib.suppress(Exception):
                    if st.mode == "cast":
                        await self.fire("Page.stopScreencast", st.sid)
                    await self.call("Target.detachFromTarget", sessionId=st.sid)

    # -- frames
    def _dispatch(self, st: Stream, w: int, h: int, jpg: bytes) -> bool:
        subs = self.subscribers(st.tid)
        ready = False
        for c in subs:
            ready = ready or c.has_capacity()
            c.offer(st.tid, (w, h, jpg))
        st.frames += 1
        st.bytes += len(jpg)
        st.last = (w, h, jpg)
        return ready or not subs

    def replay(self, c: "Client") -> None:
        """Give a window that just joined (or changed its view) the newest frame we already have. A static page,
        a login form for instance, produces no new frame on its own, so without this a reload shows a blank canvas."""
        for tid in c.wanted():
            st = self.streams.get(tid)
            if st and st.last:
                c.offer(tid, st.last)

    def _on_cast_frame(self, st: Stream, p: Dict[str, Any]) -> None:
        md = p["metadata"]
        ready = self._dispatch(st, int(md["deviceWidth"]), int(md["deviceHeight"]), base64.b64decode(p["data"]))
        st.holds.append(p["sessionId"])
        if ready:
            self._release_hold(st)      # releases this frame and any earlier one still owed
        elif st.hold_timer is None:     # every watcher is busy: make the browser wait (briefly) instead of producing stale frames
            st.hold_timer = asyncio.get_running_loop().call_later(HOLD_TIMEOUT, self._release_hold, st)

    def _clear_hold(self, st: Stream) -> None:
        if st.hold_timer:
            st.hold_timer.cancel()
        st.holds, st.hold_timer = [], None

    def _release_hold(self, st: Stream) -> None:
        if st.holds and st.sid:
            owed = st.holds
            self._clear_hold(st)
            for fid in owed:
                asyncio.create_task(self.fire("Page.screencastFrameAck", st.sid, sessionId=fid))

    def release_holds(self) -> None:
        for st in list(self.streams.values()):
            if st.holds and any(len(c.inflight) < c.WINDOW for c in self.subscribers(st.tid)):
                self._release_hold(st)

    async def _snap(self, st: Stream) -> None:
        if any(len(c.pending) > 0 for c in self.subscribers(st.tid)):
            return
        m = await self.call("Page.getLayoutMetrics", st.sid)
        vp = m.get("cssVisualViewport") or m.get("visualViewport") or {}
        cw, ch = int(vp.get("clientWidth") or 800), int(vp.get("clientHeight") or 600)
        q, mw, _mh = LEVELS["thumb"]
        r = await self.call("Page.captureScreenshot", st.sid, format="jpeg", quality=q,
                            clip={"x": 0, "y": 0, "width": cw, "height": ch, "scale": min(1.0, mw / max(cw, 1))})
        self._dispatch(st, cw, ch, base64.b64decode(r["data"]))

    # -- input (only into a tab this client is actively viewing in single mode)
    async def handle_input(self, c: Client, m: Dict[str, Any]) -> None:
        tid = m.get("tab") if isinstance(m.get("tab"), str) else c.main_tid()
        st = self.streams.get(tid) if tid else None
        if not st or not st.sid or st.mode != "cast" or c.view != "single" or tid != c.main_tid():
            return
        t, sid = m.get("t"), st.sid
        quiet = time.monotonic() - c._main_at < INPUT_QUIET
        if quiet and (t in ("key", "text", "reload") or (t == "mouse" and m.get("type") in ("mousePressed", "mouseReleased"))):
            return      # the window just switched tabs by itself: what the user typed was meant for the previous page
        if t == "mouse" and m.get("type") in ("mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"):
            p: Dict[str, Any] = dict(type=m["type"], x=num(m.get("x")), y=num(m.get("y")))
            btns, mods = int(num(m.get("buttons"))) & 7, int(num(m.get("mod"))) & 15
            if mods:
                p["modifiers"] = mods
            if m["type"] == "mouseWheel":
                p.update(deltaX=num(m.get("deltaX")), deltaY=num(m.get("deltaY")))
            elif m["type"] == "mouseMoved":
                p.update(button="left" if btns & 1 else "right" if btns & 2 else "middle" if btns & 4 else "none", buttons=btns)
            else:
                b = m.get("button") if m.get("button") in ("left", "middle", "right") else "left"
                p.update(button=b, buttons=btns, clickCount=int(num(m.get("clickCount"), 1)))
            await self.fire("Input.dispatchMouseEvent", sid, **p)
        elif t == "key" and m.get("type") in ("keyDown", "keyUp"):
            p = dict(type=m["type"], key=str(m.get("key", ""))[:32], code=str(m.get("code", ""))[:32],
                     windowsVirtualKeyCode=int(num(m.get("vk"))))
            if int(num(m.get("mod"))) & 15:
                p["modifiers"] = int(num(m.get("mod"))) & 15
            if m.get("text"):
                p["text"] = str(m["text"])[:8]
            if m.get("commands") == ["selectAll"]:
                p["commands"] = ["selectAll"]
            await self.fire("Input.dispatchKeyEvent", sid, **p)
        elif t == "text" and isinstance(m.get("text"), str):
            await self.fire("Input.insertText", sid, text=m["text"][:2000])
        elif t == "reload":
            await self.fire("Page.reload", sid)

    def stats(self) -> Dict[str, Any]:
        return {
            "instance": self.name, "tabs": len(self.tabs), "active": self.active,
            "streams": {t: {"level": s.level, "mode": s.mode, "frames": s.frames, "bytes": s.bytes, "cdp_session": bool(s.sid)}
                        for t, s in self.streams.items()},
            "clients": [{"view": c.view, "level": c.level(), "q": c.q, "sent": c.sent, "bytes": c.bytes, "drops": c.drops,
                         "rtt_ms": None if c.rtt is None else round(c.rtt * 1000, 1)} for c in self.clients],
        }


# --------------------------------------------------------------------------- http layer
SESSION_SECONDS = int(_env_float("BV_SESSION_HOURS", 168.0, 0.01, 24 * 365) * 3600)   # how long a signed-in browser stays in
AUTH_RECHECK = _env_float("BV_AUTH_RECHECK", 5.0, 0.2, 60.0)                          # seconds between credential re-checks of an open stream


def _b(s: str) -> bytes:
    """Bytes of a string that came from the network: lone surrogates (non-UTF-8 input) must never raise."""
    return s.encode("utf-8", "replace")


def _sign(exp: int, nonce: str, master: str) -> str:
    return hmac.new(_b(master), _b(f"{exp}.{nonce}"), hashlib.sha256).hexdigest()


def make_session(master: str) -> str:
    exp, nonce = int(time.time()) + SESSION_SECONDS, secrets.token_urlsafe(9)
    return f"{exp}.{nonce}.{_sign(exp, nonce, master)}"


def session_ok(value: str, master: str) -> bool:
    try:
        exp_s, nonce, sig = value.split(".", 2)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and secrets.compare_digest(_b(sig), _b(_sign(exp, nonce, master)))


def _auth_method(request: web.Request) -> Optional[str]:
    """How the request proves itself: a signed session cookie (what the page uses), the master token in the
    X-BV-Token header (scripts, tests), or not at all."""
    master = request.app["token_fn"]()
    if not master:
        return None
    cookie = request.cookies.get(request.app["cookie_name"], "")
    if cookie and session_ok(cookie, master):
        return "cookie"
    tok = request.headers.get("X-BV-Token", "")
    return "header" if tok and secrets.compare_digest(_b(tok), _b(master)) else None


def _authed(request: web.Request) -> bool:
    return _auth_method(request) is not None


def _origin_ok(request: web.Request, required: bool = False) -> bool:
    """Browsers always send Origin on WebSocket and cross-origin requests: it must be exactly this server (scheme
    and host). Non-browser clients send none, which only token-header clients may rely on (required=False)."""
    origin = request.headers.get("Origin")
    if not origin:
        return not required
    return origin.rstrip("/") == f"{request.scheme}://{request.headers.get('Host', '')}"


CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# No HSTS on purpose: it applies to the whole host name, every port, and could break other services on it.
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Content-Security-Policy": CSP,
                    "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin"}


def _harden(resp: web.StreamResponse) -> None:
    if not resp.prepared:
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)


@web.middleware
async def guard(request: web.Request, handler):
    """Refuse requests addressed to a host name this server does not serve (DNS rebinding), add security headers."""
    raw = request.headers.get("Host", "").lower()
    host = raw[:raw.index("]") + 1] if raw.startswith("[") and "]" in raw else raw.rsplit(":", 1)[0]
    if host.rstrip(".") not in request.app["allowed_hosts"]:
        refused = web.HTTPMisdirectedRequest(text="unknown host")
        _harden(refused)
        raise refused
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        _harden(exc)
        raise
    _harden(resp)
    return resp


async def _auth_guard(request: web.Request, ws: web.WebSocketResponse) -> None:
    """An open stream must not outlive its credential: a rotated master secret or an expired cookie closes it."""
    while not ws.closed:
        await asyncio.sleep(AUTH_RECHECK)
        try:
            ok = _authed(request)
        except Exception:  # noqa: BLE001 - when the check itself fails, the stream ends: never fail open
            ok = False
        if not ok:
            await ws.close(code=4401, message=b"signed out")
            return


async def index(request: web.Request) -> web.Response:
    # The page is static and holds no secret. A link carries a one-time ?b= token that the page exchanges for a
    # session cookie (POST /api/session); serving the shell without any token lets a reload work afterwards, and
    # a prefetcher that merely GETs the link cannot use the token up.
    return web.Response(body=request.app["index_html"], content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def session_login(request: web.Request) -> web.Response:
    """Exchange a one-time login token for an HttpOnly session cookie. Same-origin browser requests only."""
    if not _origin_ok(request, required=True):
        raise web.HTTPForbidden(text="origin")
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body is a plain 400
        raise web.HTTPBadRequest() from None
    tok = data.get("b") if isinstance(data, dict) else None
    if not bv_core.consume_bootstrap(tok):
        raise web.HTTPForbidden(text="this link has expired or was already used")
    master = request.app["token_fn"]()
    resp = web.json_response({"ok": True})
    resp.set_cookie(request.app["cookie_name"], make_session(master), max_age=SESSION_SECONDS,
                    httponly=True, secure=request.secure, samesite="Strict", path="/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def auth_check(request: web.Request) -> web.Response:
    if not _authed(request):
        raise web.HTTPForbidden()
    return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})


async def stats(request: web.Request) -> web.Response:
    if not _authed(request):
        raise web.HTTPForbidden()
    return web.json_response([h.stats() for h in request.app["hubs"].values() if not h.closed])


async def get_hub(app: web.Application, name: str) -> Hub:
    async with app["hub_locks"].setdefault(name, asyncio.Lock()):
        hub = app["hubs"].get(name)
        if hub and not hub.closed:
            return hub
        inst = bv_core.load_instances().get(name)
        if not inst:
            raise bv_core.ViewerError(f"unknown browser '{name}'")
        hub = Hub(name, bv_core._loopback_url(inst["cdp"]))
        try:
            await hub.start()
        except Exception:
            await hub.close()
            raise bv_core.ViewerError(f"browser '{name}' is not responding")
        app["hubs"][name] = hub
        return hub


async def ws_handler(request: web.Request) -> web.StreamResponse:
    method = _auth_method(request)
    if not method:
        raise web.HTTPForbidden()
    if not _origin_ok(request, required=(method == "cookie")):     # a browser always sends Origin; only the token header may omit it
        raise web.HTTPForbidden(text="origin")
    app = request.app
    ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=20)
    await ws.prepare(request)
    names = sorted(bv_core.load_instances())
    name = request.query.get("i") or (names[0] if names else "")
    try:
        if not name:
            raise bv_core.ViewerError("no browser registered: start one with 'hermes browser-viewer start'")
        hub = await get_hub(app, name)
        if len(hub.clients) >= MAX_CLIENTS_PER_HUB:
            raise bv_core.ViewerError("too many windows open on this browser")
    except bv_core.ViewerError as exc:
        await ws.send_str(json.dumps({"t": "err", "u": str(exc), "instances": names}))
        await ws.close()
        return ws
    client = Client(ws, hub)
    client.note_main(False)
    hub.clients.add(client)
    if hub._close_timer:
        hub._close_timer.cancel()
        hub._close_timer = None
    app["seen"] = time.time()
    sender = asyncio.create_task(client.sender())
    guard_task = asyncio.create_task(_auth_guard(request, ws))
    print(f"viewer: window opened on '{name}' ({len(hub.clients)} open)", flush=True)
    try:
        await client.send_json({"t": "hello", "instance": name, "instances": names, "levels": ORDER[1:]})
        await client.send_json(hub.tabs_msg())
        hub.replay(client)
        hub.dirty.set()
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            app["seen"] = time.time()
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            t = m.get("t")
            if t == "ack":
                client.on_ack(int(num(m.get("f"))) & 0xFFFF)
            elif t == "view":
                client.view = "mosaic" if m.get("mode") == "mosaic" else "single"
                pin = m.get("tab")
                client.pin = pin if isinstance(pin, str) and pin in hub.tabs else None
                q = m.get("q")
                client.q = q if q in ("low", "med", "high") else "auto"
                client.note_main(False)
                hub.replay(client)
                hub.dirty.set()
            elif t in ("mouse", "key", "text", "reload"):
                await hub.handle_input(client, m)
    finally:
        hub.clients.discard(client)
        sender.cancel()
        guard_task.cancel()
        print(f"viewer: window closed on '{name}' ({len(hub.clients)} open)", flush=True)
        hub.dirty.set()
        if not hub.clients and not hub.closed:   # free the CDP connection when nobody watches
            hub._close_timer = asyncio.get_running_loop().call_later(
                CLOSE_GRACE, lambda: asyncio.create_task(hub.close()) if not hub.clients else None)
    return ws


async def idle_watchdog(app: web.Application, idle_minutes: int) -> None:
    while True:
        await asyncio.sleep(10)
        busy = any(h.clients for h in app["hubs"].values() if not h.closed)
        if busy:
            app["seen"] = time.time()
        elif idle_minutes and time.time() - app["seen"] > idle_minutes * 60:
            print("viewer: idle, exiting", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)
            return


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", required=True)
    ap.add_argument("--port", type=int, default=bv_core.DEFAULT_SERVER_PORT)
    ap.add_argument("--idle", type=int, default=60, help="exit after N minutes with no window open (0 = never)")
    ap.add_argument("--tls-host", action="append", default=[], help="serve this --host over HTTPS (needs --cert, --key)")
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--tls-name", help="host name the certificate covers; used in links")
    ap.add_argument("--tls-name-host", help="the one --tls-host address that the certificate's name resolves to")
    ap.add_argument("--allow-host", action="append", default=[], help="extra Host name to serve (reverse proxy, MagicDNS name)")
    a = ap.parse_args()
    if set(a.tls_host) - set(a.host):
        sys.exit("--tls-host must also be given as --host")
    ssl_ctx = None
    if a.tls_host:
        if not (a.cert and a.key and a.tls_name):
            sys.exit("--tls-host needs --cert, --key and --tls-name")
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(a.cert, a.key, password=lambda: b"")   # an encrypted key fails, it never prompts
    for h in a.host:
        try:
            wildcard = ipaddress.ip_address(h).is_unspecified
        except ValueError:
            wildcard = True
        if wildcard:
            sys.exit("refusing to bind to a wildcard or invalid address")

    token_cache = {"mtime": 0.0, "val": ""}

    def token() -> str:   # re-read when rotated, without restarting
        p = bv_core.home() / "token"
        try:
            mt = p.stat().st_mtime
        except OSError:
            return secrets.token_urlsafe(32)   # no token file: nothing can match
        if mt != token_cache["mtime"]:
            token_cache.update(mtime=mt, val=p.read_text().strip())
        return token_cache["val"]

    bv_core.get_token()
    app = web.Application(middlewares=[guard])
    app["allowed_hosts"] = ({h.lower() for h in a.host} | {"localhost"} | {n.lower() for n in a.allow_host}
                            | ({a.tls_name.lower()} if a.tls_name else set()))
    app["token_fn"] = token
    app["cookie_name"] = f"bvs_{a.port}"      # cookies are not isolated by port, so the name carries it
    app["hubs"], app["hub_locks"], app["seen"] = {}, {}, time.time()
    app["index_html"] = (Path(__file__).resolve().parent / "web" / "index.html").read_bytes()
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/healthz", healthz), web.get("/api/stats", stats),
                    web.get("/api/auth", auth_check), web.post("/api/session", session_login)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    for h in a.host:
        await web.TCPSite(runner, h, a.port, ssl_context=ssl_ctx if h in a.tls_host else None).start()
    info = {"pid": os.getpid(), "port": a.port, "hosts": a.host, "idle_minutes": a.idle, "started": int(time.time()),
            "starttime": bv_core._proc_start(os.getpid())}
    if a.tls_host:
        info["tls"] = {"hosts": a.tls_host, "name": a.tls_name, "name_host": a.tls_name_host}
    sj = bv_core.home() / "server.json"
    bv_core._write_private(sj, json.dumps(info))
    print(f"viewer: listening on {', '.join(f'{h}:{a.port}' + (' (https)' if h in a.tls_host else '') for h in a.host)}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    wd = asyncio.create_task(idle_watchdog(app, a.idle))
    await stop.wait()
    wd.cancel()
    for hub in list(app["hubs"].values()):
        await hub.close()
    with contextlib.suppress(OSError):
        sj.unlink()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
