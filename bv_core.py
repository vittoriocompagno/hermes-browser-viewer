"""browser-viewer core: instance registry plus Chromium / viewer-server process management.

Standard library only on Linux (process facts come from /proc). On macOS they come from psutil, which Hermes
already ships. No relative imports: it is imported by the plugin (``from . import bv_core``) and by the server
process (``import bv_core``).

State lives in ``$BROWSER_VIEWER_HOME`` or ``<hermes home>/plugin-data/browser-viewer``:
    instances.json   registry of named browsers (managed = we launched it, external = attached)
    token            master secret of the viewer (0600); signs session cookies, never put in a link
    bootstrap/       one-time login tokens (SHA-256 file names, 0700)
    server.json      pid / port / hosts of the running viewer server (written by the server)
    server.log       viewer server output (never contains the token, keystrokes or pasted text)
    profiles/<name>  Chromium profile of a managed instance (cookies live here)
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_SERVER_PORT = 9281   # 9280 is left to the legacy login-viewer
FIRST_CDP_PORT = 9222
START_TIMEOUT = float(os.environ.get("BV_START_TIMEOUT", "15"))   # seconds to wait for a new Chromium
LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")
LINUX = sys.platform.startswith("linux")


def _psutil():
    """psutil for process facts where there is no /proc (macOS). Hermes depends on it, so it is normally there."""
    try:
        import psutil
    except ImportError as exc:
        raise ViewerError("psutil is required on this platform (pip install psutil)") from exc
    return psutil


class ViewerError(Exception):
    """A user-facing error: the message is safe to show."""


# --------------------------------------------------------------------------- paths / state
def hermes_home() -> Path:
    """Home of the ACTIVE profile (never read from os.environ: one process can serve several profiles)."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:  # detached server process or tests outside Hermes
        return Path("~/.hermes").expanduser()


def home() -> Path:
    env = os.environ.get("BROWSER_VIEWER_HOME")  # set by the plugin for its own detached server, or by tests
    if env:
        p = Path(env).expanduser()
    else:
        try:
            from plugins.plugin_storage import plugin_data_dir
            p = plugin_data_dir("browser-viewer")
        except ImportError:
            p = hermes_home() / "plugin-data" / "browser-viewer"
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked():
    fd = os.open(home() / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_instances() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((home() / "instances.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_instances(data: Dict[str, Dict[str, Any]]) -> None:
    _write_private(home() / "instances.json", json.dumps(data, indent=2, sort_keys=True))


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ViewerError("invalid name: use a-z, 0-9, '-' or '_' (max 32 characters, must start with a letter or digit)")
    return name


# --------------------------------------------------------------------------- tiny http helpers
def _loopback_url(url: str) -> str:
    """Accept only http://127.0.0.1:PORT-style CDP endpoints: this plugin never reaches other hosts."""
    m = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):([0-9]{2,5})/?", str(url).strip())
    if not m:
        raise ViewerError("the CDP endpoint must be http://127.0.0.1:<port> (loopback only)")
    return f"http://{m.group(1)}:{m.group(2)}"


# Every probe of ours is local (a browser's CDP port, our own server): it must never be handed to a proxy that
# http_proxy / https_proxy name, which would see the request and could not reach 127.0.0.1 anyway.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, timeout: float = 2.0) -> Any:
    with _LOCAL.open(url, timeout=timeout) as r:  # noqa: S310 - loopback or own server only
        return json.loads(r.read().decode())


def cdp_alive(cdp: str) -> bool:
    try:
        return bool(http_json(cdp + "/json/version", 1.5).get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _origin(url: str) -> str:
    """Scheme and host of a tab, never its path or query (which can carry codes and reset links)."""
    try:
        u = urlsplit(str(url))
        host, port = u.hostname, u.port        # never netloc: it carries user:password@
    except ValueError:
        return ""
    if u.scheme in ("http", "https") and host:
        return f"{u.scheme}://" + (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    return f"{u.scheme}:" if u.scheme else ""


_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "XDG_RUNTIME_DIR",
             "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
             "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP",
             "DESKTOP_SESSION", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")


def _clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """What a child process gets: the basics a browser needs, not the API keys and tokens of the Hermes process."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith(("BV_", "BROWSER_VIEWER_"))}
    env.update(extra or {})
    return env


def cdp_tabs(cdp: str) -> List[Dict[str, Any]]:
    try:
        return [t for t in http_json(cdp + "/json/list", 1.5)
                if t.get("type") == "page" and not str(t.get("url", "")).startswith(("chrome://", "devtools://", "chrome-extension://"))]
    except Exception:
        return []


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    if not LINUX:
        ps = _psutil()
        try:
            return ps.Process(int(pid)).status() != ps.STATUS_ZOMBIE
        except ps.NoSuchProcess:
            return False
        except ps.Error:
            return True
    try:  # an exited child nobody has reaped yet is a zombie: not alive for our purposes
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _proc_start(pid: Optional[int]) -> Optional[int]:
    """Start time of a process, an integer that identifies it across pid reuse: clock ticks since boot (field 22 of
    /proc/<pid>/stat) on Linux, hundredths of a second since the epoch elsewhere."""
    if not LINUX:
        ps = _psutil()
        try:
            return round(ps.Process(int(pid)).create_time() * 100)
        except (ps.Error, ValueError, TypeError):
            return None
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _proc_args(pid: Optional[int]) -> List[str]:
    if not LINUX:
        ps = _psutil()
        try:
            return [a for a in ps.Process(int(pid)).cmdline() if a]
        except (ps.Error, ValueError, TypeError):
            return []
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except (OSError, ValueError, TypeError):
        return []


def _is_our_browser(inst: Dict[str, Any]) -> bool:
    """True only if the registered pid is still the Chromium we launched for this profile (not a recycled pid)."""
    pid, prof = inst.get("pid"), inst.get("profile")
    # Chromium rewrites its own argv into ONE space-separated string, so match on the joined line
    if not pid or not prof or not re.search(re.escape(f"--user-data-dir={prof}") + r"(?:\s|$)", " ".join(_proc_args(pid))):
        return False
    started = inst.get("starttime")
    return started is None or _proc_start(pid) == started


def _kill_group(pid: int) -> bool:
    """SIGTERM then SIGKILL the browser's own process group (it is a session leader); True when it is gone."""
    try:
        send = os.killpg if os.getpgid(pid) == pid else os.kill
    except ProcessLookupError:
        return True
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            send(pid, sig)
        t0 = time.time()
        while time.time() - t0 < wait and _pid_alive(pid):
            time.sleep(0.1)
        if not _pid_alive(pid):
            return True
    return False


# --------------------------------------------------------------------------- chromium
def _chromium_candidates() -> List[str]:
    """Installed browsers in order of preference: explicit BV_CHROMIUM, system ones, Hermes' bundled one last."""
    out: List[str] = []
    env = os.environ.get("BV_CHROMIUM")
    if env and os.access(env, os.X_OK):
        out.append(env)
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "chrome"):
        w = shutil.which(name)
        if w and w not in out:
            out.append(w)
    if sys.platform == "darwin":         # app bundles are not on PATH; same places Hermes looks
        for root in ("/Applications", str(Path("~/Applications").expanduser())):
            for app in ("Google Chrome.app/Contents/MacOS/Google Chrome", "Chromium.app/Contents/MacOS/Chromium"):
                cand = f"{root}/{app}"
                if os.access(cand, os.X_OK) and cand not in out:
                    out.append(cand)
    for cand in sorted(glob.glob(str(hermes_home() / "tools" / "chromium-*" / "chrome-linux64" / "chrome")), reverse=True):
        if os.access(cand, os.X_OK) and cand not in out:
            out.append(cand)
    return out


_MAJORS: Dict[str, int] = {}


def _major(exe: str) -> int:
    if exe not in _MAJORS:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)  # noqa: S603
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", r.stdout)
            _MAJORS[exe] = int(m.group(1)) if m else 0
        except (OSError, subprocess.SubprocessError):
            _MAJORS[exe] = 0
    return _MAJORS[exe]


def _profile_major(profile: Optional[Path]) -> int:
    """Major version of the browser that last used this profile (0 = new or unknown)."""
    if not profile:
        return 0
    try:
        return int((Path(profile) / "Last Version").read_text().strip().split(".")[0])
    except (OSError, ValueError):
        return 0


def chromium_path(profile: Optional[Path] = None) -> Optional[str]:
    """Pick a browser. A profile written by a newer Chrome must not be opened by an older one (it crashes)."""
    cands = _chromium_candidates()
    need = _profile_major(profile)
    if not cands or not need:
        return cands[0] if cands else None
    for c in cands:
        if _major(c) >= need:
            return c
    raise ViewerError(f"the profile was last used by Chrome {need}: no installed browser is as recent "
                      "(set BV_CHROMIUM or use another name/profile)")


def instance_info(name: str, inst: Dict[str, Any]) -> Dict[str, Any]:
    alive = cdp_alive(inst["cdp"])
    return {
        "name": name, "cdp": inst["cdp"], "managed": bool(inst.get("managed")), "alive": alive,
        "pid": inst.get("pid"), "profile": inst.get("profile"),
        "tabs": [{"origin": _origin(t.get("url", "")), "title": str(t.get("title") or "")[:60]} for t in cdp_tabs(inst["cdp"])] if alive else [],
    }


def list_instances() -> List[Dict[str, Any]]:
    return [instance_info(n, i) for n, i in sorted(load_instances().items())]


def _session_pids(sid: int) -> List[int]:
    """Processes of the session we started with start_new_session: a wrapper that forks still counts as ours."""
    if not LINUX:
        ps = _psutil()
        found = []
        for p in ps.pids():
            with contextlib.suppress(OSError):
                if os.getsid(p) == sid:
                    found.append(p)
        return found or [sid]
    out = []
    try:
        entries = [d.name for d in Path("/proc").iterdir() if d.name.isdigit()]
    except OSError:
        return [sid]
    for name in entries:
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()      # comm may contain spaces and parentheses
            if int(fields[3]) == sid:                         # field 6 of the line is the session id
                out.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return out or [sid]


def _owns_port(port: int, pid: int) -> bool:
    """True if a process we started holds the listening socket on 127.0.0.1:`port`, not a stranger that got there
    first. Only the IPv4 loopback row counts: that is the address we connect to, and a Chromium that lost the
    IPv4 address to a stranger falls back to [::1], which must not vouch for the stranger's answer."""
    if not LINUX:
        ps = _psutil()
        for p in _session_pids(pid):
            try:
                proc = ps.Process(p)
                conns = proc.net_connections(kind="tcp4") if hasattr(proc, "net_connections") else proc.connections(kind="tcp4")
            except ps.Error:
                continue
            if any(c.status == ps.CONN_LISTEN and c.laddr and c.laddr.ip == "127.0.0.1" and c.laddr.port == port
                   for c in conns):
                return True
        return False
    inodes = set()
    try:
        rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return False
    for row in rows:
        f = row.split()
        if len(f) > 9 and f[3] == "0A" and f[1].upper() == f"0100007F:{port:04X}":      # 0A = LISTEN
            inodes.add(f[9])
    if not inodes:
        return False
    for p in _session_pids(pid):
        try:
            with os.scandir(f"/proc/{p}/fd") as fds:
                for fd in fds:
                    with contextlib.suppress(OSError):
                        tgt = os.readlink(fd.path)
                        if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                            return True
        except OSError:
            continue
    return False


def start_instance(name: str, url: str = "about:blank", port: Optional[int] = None, headless: bool = True,
                   size: str = "1280,900", profile: Optional[str] = None) -> Dict[str, Any]:
    """Launch (or reuse) a managed Chromium with a persistent profile and CDP on loopback."""
    check_name(name)
    if not re.fullmatch(r"[0-9]{3,4},[0-9]{3,4}", size):
        raise ViewerError("size must be WIDTH,HEIGHT (e.g. 1280,900)")
    if not re.match(r"(https?://|about:blank\Z)", url):
        raise ViewerError("url must start with http:// or https://")
    with _locked():
        reg = load_instances()
        cur = reg.get(name)
        if cur and cdp_alive(cur["cdp"]):
            return {**instance_info(name, cur), "reused": True}
        prof = Path(profile).expanduser() if profile else home() / "profiles" / name
        exe = chromium_path(prof)
        if not exe:
            raise ViewerError("no Chromium found (set BV_CHROMIUM or install chromium)")
        used = {int(i["port"]) for n, i in reg.items() if n != name}
        if port is None:
            port = next((p for p in range(FIRST_CDP_PORT, FIRST_CDP_PORT + 200) if p not in used and _port_free(p)), None)
            if port is None:
                raise ViewerError("no free debug port")
        elif port in used or not _port_free(port):
            raise ViewerError(f"port {port} is in use")
        prof.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(prof, 0o700)
        args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                "--no-default-browser-check", f"--window-size={size}",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows"]
        if headless:
            args.append("--headless=new")
        args.append(url)
        with open(home() / f"chromium-{name}.log", "ab") as log:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,  # noqa: S603
                                    env=_clean_env())
        cdp = f"http://127.0.0.1:{port}"
        err = None
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:     # checked first: an answer on the port must come from OUR process
                err = f"Chromium exited immediately (code {proc.returncode}); see chromium-{name}.log"
                break
            if cdp_alive(cdp) and _owns_port(port, proc.pid):    # the answer must come from OUR browser
                break
            time.sleep(0.15)
        else:
            err = "Chromium is not responding on the debug port"
        if err:
            if proc.poll() is None:
                _kill_group(proc.pid)
            proc.poll()                      # reap
            raise ViewerError(err)
        reg[name] = {"cdp": cdp, "port": port, "managed": True, "pid": proc.pid, "starttime": _proc_start(proc.pid),
                     "profile": str(prof), "created": int(time.time())}
        _save_instances(reg)
        return {**instance_info(name, reg[name]), "reused": False}


def adopt_instance(name: str, cdp: str) -> Dict[str, Any]:
    """Register an already-running browser (loopback CDP only). Hermes never stops it."""
    check_name(name)
    cdp = _loopback_url(cdp)
    if not cdp_alive(cdp):
        raise ViewerError(f"no browser is responding on {cdp}")
    with _locked():
        reg = load_instances()
        cur = reg.get(name)
        if cur and cur.get("managed") and cdp_alive(cur["cdp"]):
            raise ViewerError(f"'{name}' is a running browser managed by the plugin: choose another name")
        reg[name] = {"cdp": cdp, "port": int(cdp.rsplit(":", 1)[1]), "managed": False, "pid": None,
                     "profile": None, "created": int(time.time())}
        _save_instances(reg)
        return instance_info(name, reg[name])


def stop_instance(name: str, purge: bool = False) -> Dict[str, Any]:
    check_name(name)
    with _locked():
        reg = load_instances()
        inst = reg.get(name)
        if not inst:
            raise ViewerError(f"unknown browser '{name}'")
        killed, stale = False, False
        pid = inst.get("pid")
        if inst.get("managed") and _pid_alive(pid):
            if _is_our_browser(inst):
                killed = _kill_group(int(pid))
                if not killed:      # still running: keep the registry entry and the profile
                    raise ViewerError(f"cannot stop browser '{name}' (pid {pid}); retry or stop it by hand")
            else:
                stale = True        # the pid now belongs to something else: never signal it
        del reg[name]
        _save_instances(reg)
        purged = False
        prof = inst.get("profile")
        if purge and prof and Path(prof).resolve().parent == (home() / "profiles").resolve():
            shutil.rmtree(prof, ignore_errors=True)
            purged = True
        out = {"name": name, "stopped": killed, "managed": bool(inst.get("managed")), "profile_deleted": purged}
        if stale:
            out["note"] = "the registered pid is no longer our browser (already closed): no process was touched"
        return out


# --------------------------------------------------------------------------- token / hosts / server
def get_token(rotate: bool = False) -> str:
    """The master secret of the viewer. It never appears in a link: it signs the session cookies, so rotating it
    signs every browser out (and drops the login links that were handed out but not used yet)."""
    p = home() / "token"
    if rotate or not p.exists():
        _write_private(p, secrets.token_urlsafe(24))
        if rotate:
            purge_bootstrap()
    return p.read_text().strip()


BOOTSTRAP_TTL = 600   # seconds a login link stays valid


def _bootstrap_dir() -> Path:
    d = home() / "bootstrap"
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def new_bootstrap(ttl: int = BOOTSTRAP_TTL) -> str:
    """A one-time login token. Only its SHA-256 is stored, as a file name; the file holds the expiry time."""
    d = _bootstrap_dir()
    now = time.time()
    for f in d.iterdir():                       # drop expired leftovers
        with contextlib.suppress(OSError, ValueError):
            if float(f.read_text()) < now:
                f.unlink()
    tok = secrets.token_urlsafe(24)
    _write_private(d / hashlib.sha256(tok.encode("utf-8", "replace")).hexdigest(), str(now + ttl))
    return tok


def consume_bootstrap(tok: str) -> bool:
    """True exactly once for a valid, unexpired token. Unlinking the file is the atomic step: of two
    simultaneous requests only one can remove it."""
    if not isinstance(tok, str) or not tok or len(tok) > 200:
        return False
    p = _bootstrap_dir() / hashlib.sha256(tok.encode("utf-8", "replace")).hexdigest()
    try:
        expires = float(p.read_text())
        p.unlink()
    except (OSError, ValueError):
        return False
    return expires >= time.time()


def purge_bootstrap() -> None:
    for f in _bootstrap_dir().iterdir():
        with contextlib.suppress(OSError):
            f.unlink()


def _tailscale_exe() -> Optional[str]:
    """The tailscale CLI: on PATH, or inside the macOS app, which does not put it on PATH."""
    exe = shutil.which("tailscale")
    mac = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not exe and sys.platform == "darwin" and os.access(mac, os.X_OK):
        exe = mac
    return exe


def tailscale_ip() -> Optional[str]:
    exe = _tailscale_exe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=3).stdout.split()  # noqa: S603
    except Exception:
        return None
    return out[0] if out and re.match(r"^\d+\.\d+\.\d+\.\d+$", out[0]) else None


def default_hosts() -> List[str]:
    env = os.environ.get("BROWSER_VIEWER_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    ts = tailscale_ip()
    return ["127.0.0.1"] + ([ts] if ts else [])


def server_info() -> Optional[Dict[str, Any]]:
    try:
        info = json.loads((home() / "server.json").read_text())
    except (OSError, ValueError):
        return None
    if not _pid_alive(info.get("pid")):
        return None
    try:
        hosts, tls_hosts = info.get("hosts") or ["127.0.0.1"], set((info.get("tls") or {}).get("hosts", []))
        h = "127.0.0.1" if "127.0.0.1" in hosts else hosts[0]
        if h in tls_hosts:      # liveness probe of our own server: the certificate names a host, not this IP
            url, ctx = f"https://{h}:{info['port']}/healthz", ssl._create_unverified_context()  # noqa: S323
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
            with opener.open(url, timeout=1.5) as r:  # noqa: S310
                json.loads(r.read().decode())
        else:
            http_json(f"http://{h}:{info['port']}/healthz", 1.5)
    except Exception:
        return None
    return info


def _python_cmd() -> List[str]:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        raise ViewerError("aiohttp is missing: install the plugin with its dependencies (hermes plugins install)") from None
    return [sys.executable]


# --------------------------------------------------------------------------- HTTPS (opt-in)
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def tls_settings() -> Optional[Dict[str, str]]:
    """The HTTPS setting chosen with `hermes browser-viewer tls`, or None for plain HTTP."""
    try:
        rec = json.loads((home() / "tls.json").read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("cert") and rec.get("key") and rec.get("name") else None


def _tailscale_cert() -> Dict[str, str]:
    """Get (or renew) a Let's Encrypt certificate for this machine's MagicDNS name through `tailscale cert`."""
    exe = _tailscale_exe()
    if not exe:
        raise ViewerError("tailscale is not installed")
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10)  # noqa: S603
        if r.returncode != 0:
            raise ViewerError("tailscale status failed: " + (r.stderr or r.stdout).strip()[:200])
        st = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ViewerError(f"cannot read the Tailscale status: {type(exc).__name__}") from exc
    name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if not name or name not in [d.lower() for d in (st.get("CertDomains") or [])]:
        raise ViewerError("this tailnet does not offer HTTPS certificates for this machine "
                          "(enable MagicDNS and HTTPS Certificates in the Tailscale admin console)")
    if not HOSTNAME_RE.fullmatch(name):
        raise ViewerError(f"unexpected machine name: {name!r}")
    d = home() / "tls"
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    cert, key = d / "cert.pem", d / "key.pem"
    try:
        r = subprocess.run([exe, "cert", "--cert-file", str(cert), "--key-file", str(key), name],  # noqa: S603
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ViewerError(f"tailscale cert did not finish: {type(exc).__name__}") from exc
    if r.returncode != 0:
        raise ViewerError("tailscale cert failed: " + (r.stderr or r.stdout).strip()[:200]
                          + " (on Linux, `tailscale cert` needs root or your user set as the Tailscale operator: see `tailscale set --help`)")
    with contextlib.suppress(OSError):
        os.chmod(key, 0o600)
    return {"mode": "tailscale", "name": name, "cert": str(cert), "key": str(key)}


def _cert_days_left(cert: str) -> Optional[float]:
    """Days until the certificate expires, or None when it cannot be read."""
    try:
        return (ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(cert)["notAfter"]) - time.time()) / 86400  # noqa: SLF001
    except Exception:  # noqa: BLE001 - an unreadable date is "unknown", not an error
        return None


def set_tls(mode: str, cert: Optional[str] = None, key: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
    """Choose how the server is reached from other devices: tailscale (certificate from tailscale cert), files
    (your own certificate and key for NAME) or off (plain HTTP). Takes effect when the server next starts."""
    tls_file = home() / "tls.json"
    if mode == "off":
        old = tls_settings()
        tls_file.unlink(missing_ok=True)
        if old and old.get("mode") == "tailscale":      # our own copy of the key; a "files" key is yours, never touched
            for f in (home() / "tls").glob("*.pem"):
                f.unlink(missing_ok=True)
        return {"tls": "off"}
    if mode == "tailscale":
        rec = _tailscale_cert()
    elif mode == "files":
        name = (name or "").strip().lower()
        if not (cert and key and HOSTNAME_RE.fullmatch(name)):
            raise ViewerError("files mode needs a certificate, a key and the host name the certificate covers (--name)")
        cert_p, key_p = Path(cert).expanduser().resolve(), Path(key).expanduser().resolve()
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_p, key_p, password=lambda: b"")
        except (OSError, ssl.SSLError) as exc:
            raise ViewerError(f"cannot load the certificate and key: {exc}") from exc
        rec = {"mode": "files", "name": name, "cert": str(cert_p), "key": str(key_p)}
    else:
        raise ViewerError(f"unknown tls mode: {mode!r} (tailscale, files, off)")
    _write_private(tls_file, json.dumps(rec))
    return {"tls": mode, "name": rec["name"], "note": "applies the next time the server starts (hermes browser-viewer stop --server)"}


def _last_log_line() -> str:
    try:
        lines = [l.strip() for l in (home() / "server.log").read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return "see server.log"
    return (lines[-1][:200] if lines else "see server.log")


def start_server(port: int = DEFAULT_SERVER_PORT, hosts: Optional[List[str]] = None, idle_minutes: int = 60) -> Dict[str, Any]:
    info = server_info()
    if info:
        return {**info, "reused": True}
    hosts = hosts or default_hosts()
    for h in hosts:
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            ip = None
        if ip is None or ip.version != 4 or ip.is_unspecified or str(ip) != h:
            raise ViewerError(f"invalid host: {h} (specific IPv4 addresses only, never 0.0.0.0)")
    get_token()
    cmd = _python_cmd() + [str(Path(__file__).with_name("bv_server.py")), "--port", str(int(port)), "--idle", str(int(idle_minutes))]
    for h in hosts:
        cmd += ["--host", h]
    for n in os.environ.get("BV_ALLOWED_HOSTS", "").split(","):
        if n.strip():
            if not HOSTNAME_RE.fullmatch(n.strip().lower()):
                raise ViewerError(f"invalid host name in BV_ALLOWED_HOSTS: {n.strip()!r}")
            cmd += ["--allow-host", n.strip().lower()]
    tls = tls_settings()
    warnings: List[str] = []
    # HTTPS on every address except loopback, which stays plain HTTP (a secure context for browsers anyway)
    tls_hosts = [h for h in hosts if not ipaddress.ip_address(h).is_loopback] if tls else []
    if tls and tls_hosts:
        if tls["mode"] == "tailscale":
            try:
                tls = _tailscale_cert()                 # no-op while valid, renews when close to expiry
                _write_private(home() / "tls.json", json.dumps(tls))
            except ViewerError as exc:                  # a Tailscale hiccup must not lock you out of a certificate you have
                left = _cert_days_left(tls["cert"])
                if not (Path(tls["cert"]).is_file() and Path(tls["key"]).is_file()) or (left is not None and left <= 0):
                    raise
                warnings.append(f"could not refresh the Tailscale certificate ({exc}); serving the one on disk"
                                + (f", {left:.0f} days left" if left is not None else ""))
        for h in tls_hosts:
            cmd += ["--tls-host", h]
        cmd += ["--cert", tls["cert"], "--key", tls["key"], "--tls-name", tls["name"]]
        ts = tailscale_ip() if tls["mode"] == "tailscale" else None
        if ts in tls_hosts:
            cmd += ["--tls-name-host", ts]
    with open(home() / "server.log", "ab") as log:  # the child keeps its own copy of the descriptor
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,  # noqa: S603
                                env=_clean_env({"BROWSER_VIEWER_HOME": str(home())}))
    for _ in range(300):  # up to 60 s
        info = server_info()
        if info:
            return {**info, "reused": False, **({"warnings": warnings} if warnings else {})}
        if proc.poll() is not None:                     # it died: say why now, not after a minute
            raise ViewerError(f"the server exited with code {proc.returncode}: {_last_log_line()}")
        time.sleep(0.2)
    raise ViewerError("the server did not start: see server.log in the plugin data directory")


def stop_server() -> Dict[str, Any]:
    try:
        info = json.loads((home() / "server.json").read_text())
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"stopped": False, "note": "server not running"}
    started = info.get("starttime")
    if (not _pid_alive(pid) or not re.search(r"bv_server\.py(?:\s|$)", " ".join(_proc_args(pid)))
            or (started and _proc_start(pid) != started)):
        with contextlib.suppress(OSError):
            (home() / "server.json").unlink()
        return {"stopped": False, "note": "server not running"}
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    t0 = time.time()
    while time.time() - t0 < 6 and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return {"stopped": True}


# --------------------------------------------------------------------------- which browser browser_exec drives
def cdp_in_use() -> Optional[str]:
    """The CDP endpoint this plugin last wrote into browser.cdp_url (None if it did not, or cleared it)."""
    try:
        return json.loads((home() / "state.json").read_text()).get("cdp_url")
    except (OSError, ValueError, AttributeError):
        return None


def set_cdp_in_use(cdp: Optional[str]) -> None:
    _write_private(home() / "state.json", json.dumps({"cdp_url": cdp}))


def links(instance: Optional[str] = None) -> List[str]:
    info = server_info()
    if not info:
        return []
    # one single-use login token per link: opening it signs that browser in with a session cookie.
    # Addresses served with TLS are reached by the certificate's host name, the others by IP over plain HTTP.
    tls = info.get("tls") or {}
    if instance:
        check_name(instance)
    out: List[str] = []
    for h in info.get("hosts", []):
        if h in tls.get("hosts", []):
            # the certificate's name only reaches the address it resolves to; another TLS address is reached by IP
            on_name = not tls.get("name_host") or tls.get("name_host") == h
            base = f"https://{tls['name'] if on_name else h}:{info['port']}"
        else:
            base = f"http://{h}:{info['port']}"
        if base not in out:
            out.append(base)
    return [f"{b}/?b={new_bootstrap()}" + (f"&i={quote(instance, safe='')}" if instance else "") for b in out]


def status() -> Dict[str, Any]:
    info = server_info()
    return {
        "server": ({"running": True, "port": info["port"], "hosts": info.get("hosts", []), "pid": info["pid"],
                    "idle_minutes": info.get("idle_minutes")} if info else {"running": False}),
        "instances": list_instances(),
        "chromium": chromium_path(),
        "data_dir": str(home()),
    }
