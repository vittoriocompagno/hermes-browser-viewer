"""browser-viewer plugin: live view and human takeover of the browser Hermes drives.

Surfaces
  tool   browser_viewer      the agent starts a viewer, hands the link to the user, switches browsers
  cli    hermes browser-viewer start|link|status|list|new|adopt|use|stop|token
  slash  /viewer             status and link from inside a chat

The streaming server (bv_server.py) is a separate detached process, so it survives Hermes sessions.
Everything stateful (browsers, profiles, token) lives in the plugin data dir; see bv_core.py.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from . import bv_core

logger = logging.getLogger(__name__)

ACTIONS = ["status", "start", "link", "list", "new", "adopt", "use", "stop", "rotate_token"]

SCHEMA = {
    "name": "browser_viewer",
    "description": (
        "Give the user a live, controllable view of the browser Hermes drives, so THEY can log in (password, 2FA, "
        "CAPTCHA) or watch you work, from another device on the tailnet. Never type credentials yourself: use this "
        "and let the user type them in the page. The link opens a web page that streams the browser tabs and "
        "forwards the user's mouse, keyboard and paste. Several windows can watch the same or different tabs, and "
        "several named browsers can exist at once (each with its own persistent profile, e.g. one per account).\n"
        "Actions: start = make sure the viewer server and a browser exist and return the link (use first); "
        "link = just the link; status / list = what is running; new = another named browser; adopt = register an "
        "already running browser by its loopback CDP URL; use = point browser_exec at a browser (sets "
        "browser.cdp_url, which affects EVERY Hermes session (other chats, cron jobs) until cleared; only when the "
        "user asks; then open a NEW browser_exec session name); use with name 'clear' = back to the default clean "
        "browser; stop = stop a browser (also clears browser.cdp_url if it pointed there) or the server; "
        "rotate_token = new link.\n"
        "After 'start', give the user the link and tell them to log in; then call use, and continue with browser_exec. "
        "The link contains a secret token: share it only with the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ACTIONS, "description": "What to do."},
            "name": {"type": "string", "description": "Browser name (a-z, 0-9, - _). Default 'login'. For use: 'clear' resets."},
            "url": {"type": "string", "description": "Page to open when the browser is created (http/https)."},
            "cdp_url": {"type": "string", "description": "For adopt: http://127.0.0.1:<port> of a running Chromium."},
            "what": {"type": "string", "enum": ["browser", "server"], "description": "For stop: default 'browser'."},
            "purge": {"type": "boolean", "description": "For stop: also delete the browser profile (cookies, logins)."},
            "size": {"type": "string", "description": "Window size for a new browser, e.g. 1280,900."},
        },
        "required": ["action"],
    },
}


# --------------------------------------------------------------------------- shared logic
def _hermes_bin() -> str:
    return shutil.which("hermes") or str(Path("~/.local/bin/hermes").expanduser())


def _set_cdp_url(cdp: str | None) -> str:
    cmd = [_hermes_bin(), "config"] + (["set", "browser.cdp_url", cdp] if cdp else ["unset", "browser.cdp_url"])
    try:
        env = dict(os.environ, HERMES_HOME=str(bv_core.hermes_home()))  # the ACTIVE profile's config, not whichever the process env names
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise bv_core.ViewerError(f"cannot run 'hermes config': {type(exc).__name__}") from exc
    if r.returncode != 0:
        raise bv_core.ViewerError("hermes config failed: " + (r.stderr or r.stdout).strip()[:200])
    return (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "ok"


def _links_payload(name: str | None) -> Dict[str, Any]:
    info = bv_core.server_info()
    urls = bv_core.links(name)
    out: Dict[str, Any] = {"links": urls}
    if info:
        hosts = info.get("hosts", [])
        if hosts == ["127.0.0.1"]:
            out["warning"] = ("the server listens on loopback only (no Tailscale IP found): "
                              "the link works only on this machine")
        out["note"] = ("the link contains a secret token and stays valid until you rotate it; "
                       f"the server shuts down after {info.get('idle_minutes') or '∞'} minutes with no open window")
    return out


def run_action(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action", "")).strip()
    name = (args.get("name") or "").strip() or None
    if action not in ACTIONS:
        raise bv_core.ViewerError(f"azione sconosciuta: {action!r} (valide: {', '.join(ACTIONS)})")

    if action == "status":
        return bv_core.status()

    if action == "list":
        return {"instances": bv_core.list_instances()}

    if action in ("start", "new"):
        n = name or "login"
        inst = bv_core.start_instance(n, url=args.get("url") or "about:blank", size=args.get("size") or "1280,900")
        out: Dict[str, Any] = {"browser": {k: inst[k] for k in ("name", "cdp", "managed", "reused", "profile")}}
        if action == "start" or not bv_core.server_info():
            srv = bv_core.start_server()
            out["server"] = {"port": srv["port"], "hosts": srv["hosts"], "reused": srv.get("reused", False)}
        out.update(_links_payload(n))
        out["next"] = ("when the user has finished logging in, use action='use' to make browser_exec drive this browser "
                       "(then open a new browser_exec session)")
        return out

    if action == "link":
        if not bv_core.server_info():
            raise bv_core.ViewerError("the server is not running: use action='start'")
        return _links_payload(name)

    if action == "adopt":
        n = name or "login"
        inst = bv_core.adopt_instance(n, str(args.get("cdp_url") or ""))
        return {"browser": {k: inst[k] for k in ("name", "cdp", "managed", "alive")},
                "note": "browser registered; Hermes will never shut it down"}

    if action == "use":
        if name in (None, "clear", "default"):
            msg = _set_cdp_url(None)
            bv_core.set_cdp_in_use(None)
            return {"browser_exec": "browser pulito predefinito", "config": msg}
        inst = bv_core.load_instances().get(bv_core.check_name(name))
        if not inst:
            raise bv_core.ViewerError(f"unknown browser '{name}'")
        if not bv_core.cdp_alive(inst["cdp"]):
            raise bv_core.ViewerError(f"browser '{name}' is not responding")
        msg = _set_cdp_url(inst["cdp"])
        bv_core.set_cdp_in_use(inst["cdp"])
        return {"browser_exec": f"browser_exec now drives '{name}' ({inst['cdp']})", "config": msg,
                "note": ("applies to every Hermes session until you call use with name='clear'; "
                         "open a NEW browser_exec session (different name) to attach to it")}

    if action == "stop":
        if (args.get("what") or "browser") == "server":
            return bv_core.stop_server()
        if not name:
            raise bv_core.ViewerError("the name of the browser to stop is required (or what='server')")
        cdp = (bv_core.load_instances().get(name) or {}).get("cdp")
        res = bv_core.stop_instance(name, purge=bool(args.get("purge")))
        if cdp and cdp == bv_core.cdp_in_use():     # browser_exec must not be left pointing at a dead endpoint
            res["browser_exec"] = "back to the default clean browser (browser.cdp_url removed)"
            _set_cdp_url(None)
            bv_core.set_cdp_in_use(None)
        return res

    if action == "rotate_token":
        bv_core.get_token(rotate=True)
        return {"rotated": True, **(_links_payload(name) if bv_core.server_info() else {"links": []})}

    raise bv_core.ViewerError("unhandled action")  # pragma: no cover


def tool_handler(args: Dict[str, Any], **_kwargs: Any) -> str:
    try:
        return json.dumps(run_action(args or {}), ensure_ascii=False, default=str)
    except bv_core.ViewerError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        logger.exception("browser_viewer failed")
        return json.dumps({"error": f"unexpected error: {type(exc).__name__}: {exc}"}, ensure_ascii=False)


# --------------------------------------------------------------------------- cli
def _fmt(res: Dict[str, Any]) -> str:
    return json.dumps(res, indent=2, ensure_ascii=False, default=str)


def _cli(args) -> None:
    cmd = getattr(args, "bv_cmd", None)
    if not cmd:
        print("usage: hermes browser-viewer <start|link|status|list|new|adopt|use|stop|token> [...]")
        return
    mapping: Dict[str, Dict[str, Any]] = {
        "start": {"action": "start", "name": getattr(args, "name", None), "url": getattr(args, "url", None),
                  "size": getattr(args, "size", None)},
        "link": {"action": "link", "name": getattr(args, "name", None)},
        "status": {"action": "status"},
        "list": {"action": "list"},
        "new": {"action": "new", "name": getattr(args, "name", None), "url": getattr(args, "url", None),
                "size": getattr(args, "size", None)},
        "adopt": {"action": "adopt", "name": getattr(args, "name", None), "cdp_url": getattr(args, "cdp", None)},
        "use": {"action": "use", "name": "clear" if getattr(args, "clear", False) else getattr(args, "name", None)},
        "stop": {"action": "stop", "name": getattr(args, "name", None), "what": "server" if getattr(args, "server", False) else "browser",
                 "purge": getattr(args, "purge", False)},
        "token": {"action": "rotate_token" if getattr(args, "rotate", False) else "link"},
    }
    try:
        res = run_action(mapping[cmd])
    except bv_core.ViewerError as exc:
        raise SystemExit(f"error: {exc}")
    links: List[str] = res.get("links", []) if isinstance(res, dict) else []
    if cmd in ("start", "link", "token") and links:
        print("Open one of these links from the device you want to control the browser from:")
        for u in links:
            print("  " + u)
        for k in ("warning", "note", "next"):
            if res.get(k):
                print(f"\n{k}: {res[k]}")
        return
    print(_fmt(res))


def _setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest="bv_cmd")
    p = sub.add_parser("start", help="start the viewer and a browser, print the link")
    p.add_argument("name", nargs="?", default="login")
    p.add_argument("--url")
    p.add_argument("--size")
    p = sub.add_parser("link", help="print the viewer link")
    p.add_argument("name", nargs="?")
    sub.add_parser("status", help="status of the server and browsers")
    sub.add_parser("list", help="registered browsers with their tabs")
    p = sub.add_parser("new", help="create another browser with its own profile")
    p.add_argument("name")
    p.add_argument("--url")
    p.add_argument("--size")
    p = sub.add_parser("adopt", help="register a browser that is already running (loopback only)")
    p.add_argument("name")
    p.add_argument("cdp", help="http://127.0.0.1:<port>")
    p = sub.add_parser("use", help="let browser_exec drive a browser (browser.cdp_url)")
    p.add_argument("name", nargs="?")
    p.add_argument("--clear", action="store_true", help="go back to the default clean browser")
    p = sub.add_parser("stop", help="stop a browser (or --server)")
    p.add_argument("name", nargs="?")
    p.add_argument("--server", action="store_true")
    p.add_argument("--purge", action="store_true", help="also delete the profile (cookies and logins)")
    p = sub.add_parser("token", help="show the link; with --rotate generate a new one")
    p.add_argument("--rotate", action="store_true")
    parser.set_defaults(func=_cli)


def _slash(raw: str) -> str:
    parts = (raw or "").split()
    try:
        if parts and parts[0] == "start":
            res = run_action({"action": "start", "name": parts[1] if len(parts) > 1 else None})
        elif parts and parts[0] == "stop":
            res = run_action({"action": "stop", "what": "server"})
        else:
            res = run_action({"action": "link"}) if bv_core.server_info() else run_action({"action": "status"})
    except bv_core.ViewerError as exc:
        return f"error: {exc}"
    return "\n".join(res.get("links", [])) or _fmt(res)


# --------------------------------------------------------------------------- registration
def register(ctx) -> None:
    ctx.register_tool(
        name="browser_viewer", toolset="browser_viewer", schema=SCHEMA, handler=tool_handler,
        check_fn=lambda: bv_core.chromium_path() is not None, emoji="🖥️",
        description="Live view and human takeover of the Hermes browser",
    )
    ctx.register_cli_command(name="browser-viewer", help="Live view / human takeover of the Hermes browser",
                             setup_fn=_setup_cli, handler_fn=_cli)
    ctx.register_command("viewer", _slash, description="Browser viewer link (start | stop)", args_hint="[start [name]|stop]")
