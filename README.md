<p align="center">
  <img alt="Hermes 0.23.1 or newer" src="https://img.shields.io/badge/Hermes-%E2%89%A5%200.23.1-1a73e8?style=flat-square">
  <img alt="plugin version 0.1.0" src="https://img.shields.io/badge/plugin-0.1.0-0a84ff?style=flat-square">
  <img alt="Linux | macOS" src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-34c759?style=flat-square">
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-8e8e93?style=flat-square">
</p>

# browser-viewer

**See the browser your Hermes agent is driving, live, from your phone or laptop, and take the keyboard when it needs you.**

Agents get stuck on logins, 2FA codes and CAPTCHAs. This plugin streams the tabs of the Chromium that Hermes controls to a web page you open from another device. You see what the agent sees, you click, type, scroll and paste yourself, and when you are done the agent carries on in the same logged-in browser.

<p align="center">
  <a href="#instructions-for-agents">For agents</a> ·
  <a href="#install">Install</a> ·
  <a href="#use">Use</a> ·
  <a href="#https">HTTPS</a> ·
  <a href="#security-model">Security</a> ·
  <a href="#disclosure">Disclosure</a> ·
  <a href="#known-limitations">Limitations</a>
</p>

## Instructions for agents

Running a Hermes agent? Copy the box and paste it into your chat. The agent installs the plugin and knows how to hand you the browser when it needs a login.

```text
Set up and use the browser-viewer plugin for Hermes
(https://github.com/vittoriocompagno/hermes-browser-viewer).

What it is for: when you hit a login, a 2FA code or a CAPTCHA in the browser, you give
me a link. I open it on my phone or laptop, see your browser live and type the
credentials myself. Then you carry on in the same logged-in browser.

Setup, once:
1. Check the requirements: Linux or macOS, Hermes 0.23.1 or newer (hermes --version) and a
   Chromium or Chrome binary (which chromium).
2. Run: hermes plugins install vittoriocompagno/hermes-browser-viewer
3. Run: hermes plugins enable browser-viewer
4. The browser_viewer tool appears in a new session. Tell me to open one if you cannot
   see it.

Whenever you need me to log in:
1. Call browser_viewer with action "start" (browser name "login" unless I say
   otherwise). It returns the login link.
2. Send me the link. It works once and expires after 10 minutes; call action "link"
   for a fresh one, for example for another device. If the only link is on 127.0.0.1,
   it opens on this machine only: tell me.
3. Wait while I log in on that page. Never type my password, my 2FA code or any other
   credential yourself, and never write the link into files, notes or memory.
4. When I say I am done, take a screenshot and check that the page shows the logged-in
   state.
5. If I want you to keep working in that browser with browser_exec, call action "use"
   with name "login", then open a NEW browser_exec session. "use" sets browser.cdp_url
   for every Hermes session, cron jobs included, so do it only because I asked or
   agreed. When we are finished, call "use" with name "clear".

Other actions: "status" and "list" show what is running, "new" starts another browser
with its own profile (one per account), "stop" stops a browser (add what="server" to
stop the streaming server), "rotate_token" signs every browser out and gives a fresh
link.
Do not change settings such as HTTPS (hermes browser-viewer tls) unless I ask. If
something fails, run hermes browser-viewer status and tell me what it says.
```

<table>
  <tr>
    <td width="68%"><img src="https://raw.githubusercontent.com/vittoriocompagno/hermes-browser-viewer/main/docs/screenshot-desktop.png" alt="The viewer on a laptop, streaming a sign-in page"></td>
    <td width="32%"><img src="https://raw.githubusercontent.com/vittoriocompagno/hermes-browser-viewer/main/docs/screenshot-phone.png" alt="The viewer on a phone, streaming the same page"></td>
  </tr>
  <tr>
    <td align="center"><sub>On a laptop</sub></td>
    <td align="center"><sub>On a phone</sub></td>
  </tr>
</table>

## How it works

```
 Hermes agent ──── CDP ───▶  Chromium (managed, persistent profile)
                                  │  JPEG screencast
                                  ▼
 you ── one-time login link ─▶  viewer server ──── WebSocket ───▶  your phone or laptop
                                                                   see, click, type, paste
```

The agent asks for a link, you open it, you log in, and the agent keeps working with the cookies you just created. The streaming server runs as its own detached process, so it outlives the chat session, and it shuts itself down after 60 minutes with no open window.

## What you get

- Several windows and several browsers at once, each browser with its own persistent profile, for example one per account.
- A follow mode: the page follows the tab the agent works on and pins to the tab you are using while you type.
- Stream quality that adapts to each client's round-trip time, or a level you fix yourself.
- Mouse, keyboard (AltGr included), drag and paste, plus touch: a tap clicks, one finger scrolls, and a keyboard button opens the phone keyboard.
- Single-use login links and an `HttpOnly` session cookie. No keystroke, pasted text or secret is written to any log.

## At a glance

| Item | Details |
|---|---|
| Hermes | 0.23.1 or newer (developed and tested on v0.23.1) |
| Plugin | 0.2.0, MIT |
| System | Linux, tested on Arch-based Omarchy, x86_64 (process checks read `/proc`). macOS on Apple Silicon, tested on GitHub's macOS runners (process checks through `psutil`, which Hermes already ships) |
| Browser | Chromium or Chrome, tested with Chromium 155 |
| Viewing device | any current browser, touch supported |
| Network | loopback, or a Tailscale address; plain HTTP by default, HTTPS opt-in |

## Install

```bash
hermes plugins install vittoriocompagno/hermes-browser-viewer     # straight from GitHub
hermes plugins install browser-viewer                             # from the Hermes catalog, once it is listed
hermes plugins enable browser-viewer
hermes browser-viewer start                                       # prints a login link: open it on your phone
```

Requirements: Hermes `>=0.23.1`, the Python dependency `aiohttp>=3.9,<4` (installed with the plugin) and a Chromium or Chrome binary on the machine (`chromium`, `google-chrome-stable`, a path in `BV_CHROMIUM`, or one Hermes installed under its `tools/` directory). On macOS it also finds Google Chrome and Chromium in `/Applications` or `~/Applications`.

## Use

From a terminal:

```bash
hermes browser-viewer start            # starts the server and the browser "login", prints the link
hermes browser-viewer link             # print the link again
hermes browser-viewer new work         # a second browser with its own profile
hermes browser-viewer adopt main http://127.0.0.1:9222   # register a Chromium that is already running (loopback only)
hermes browser-viewer use login        # make browser_exec drive this browser
hermes browser-viewer use --clear      # back to the default clean browser
hermes browser-viewer stop login       # stop a browser (add --purge to also delete its profile)
hermes browser-viewer stop --server    # stop the streaming server
hermes browser-viewer token --rotate   # sign every browser out, then print a fresh login link
hermes browser-viewer tls tailscale    # serve other devices over HTTPS with a Tailscale certificate (see below)
hermes browser-viewer tls files --cert fullchain.pem --key key.pem --name viewer.example.com   # or your own certificate
hermes browser-viewer tls off          # back to plain HTTP; `tls status` shows the current setting
```

From a chat: the `browser_viewer` tool (actions `status`, `start`, `link`, `list`, `new`, `adopt`, `use`, `stop`, `rotate_token`) and the `/viewer` slash command. The agent starts a viewer, gives you the link, and after you have logged in switches `browser_exec` to that browser with `use`.

Open the link from the device you want to control the browser from. It works once and for 10 minutes: use `link` again for another device. The server listens on the addresses you choose with `BROWSER_VIEWER_HOSTS`, by default loopback plus the machine's Tailscale IPv4 if `tailscale` is installed. It never binds `0.0.0.0`. It shuts itself down after 60 minutes with no open window.

### HTTPS

By default the server speaks plain HTTP, which is fine on loopback and on a Tailscale network (WireGuard encrypts it) and nowhere else. HTTPS is opt-in:

- `tls tailscale` asks `tailscale cert` for a Let's Encrypt certificate for this machine's MagicDNS name. It needs MagicDNS and HTTPS Certificates enabled for the tailnet. The plugin refreshes the certificate every time the server starts.
- `tls files` uses a certificate and key you already have, for the host name you pass with `--name`.

The setting takes effect when the server next starts. Every address except loopback is then served over HTTPS (TLS 1.2 or newer) and the link for the address the certificate's name resolves to uses that name (in `tailscale` mode: the Tailscale address; another TLS address is reached by IP); loopback stays plain HTTP. The session cookie then carries the `Secure` flag.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `BROWSER_VIEWER_HOSTS` | comma-separated specific IPv4 addresses to listen on | `127.0.0.1` plus the Tailscale IPv4 |
| `BV_CHROMIUM` | path to the Chromium or Chrome binary | auto-detected |
| `BV_START_TIMEOUT` | seconds to wait for a new Chromium | `15` |
| `BV_CLOSE_GRACE` | seconds an empty hub keeps its CDP connection | `20` |
| `BV_SESSION_HOURS` | how long a signed-in browser stays signed in | `168` (7 days) |
| `BV_ALLOWED_HOSTS` | extra Host names to serve besides the listening addresses, `localhost` and the certificate's name (comma-separated), for example the MagicDNS name over plain HTTP | none |
| `BV_AUTH_RECHECK` | seconds between re-checks of the credential of an open stream | `5` |
| `BROWSER_VIEWER_HOME` | override of the data directory (the plugin sets it for its own server process; useful for tests) | the plugin data dir of the active profile |

State lives in the plugin data directory (`<hermes home>/plugin-data/browser-viewer/`): browser registry, profiles, master secret, pending login tokens, logs.

## Security model

- Whoever holds the link controls a browser that may be logged in to your accounts. Treat the link like a password.
- A link carries a one-time login token (192 random bits, valid 10 minutes). The page sends it with a same-origin `POST /api/session` and receives an `HttpOnly; SameSite=Strict` session cookie (named after the server port, 7 days by default). Merely opening or prefetching the URL does not use the token up, only that POST does; the token is then removed from the address bar. Only its SHA-256 is stored on the server, in a `0700` directory, and consuming it is an atomic file removal, so two simultaneous requests cannot both succeed.
- The cookie is an HMAC-SHA256 signature over its expiry, keyed with a master secret kept in a `0600` file. The master secret never appears in a link, a page or a tool result. Rotating it (`rotate_token`, `token --rotate`) signs every browser out and drops login links that were not used yet, without restarting the server. An open stream is re-checked every few seconds (`BV_AUTH_RECHECK`) and is closed as soon as its cookie has expired or the secret has changed, so a signed-out browser stops receiving frames within seconds. Scripts can present the master secret in the `X-BV-Token` header; it is not accepted in a URL.
- The cookie carries the `Secure` flag on every address served over HTTPS (not on loopback, which stays plain HTTP).
- The WebSocket and the stats endpoint need the cookie (or the master secret). The static page is served without either and contains no secret.
- Managed browsers open their CDP port on `127.0.0.1` only, and a new one is registered only if a process of the session the plugin just started holds the IPv4 loopback debug port, so a stranger that got there first is never mistaken for the browser. Adopted browsers must be loopback endpoints (the plugin can enforce where it connects, not what they listen on), and the plugin never kills a browser it did not start.
- Requests must be addressed to a host name this server serves (its listening addresses, `localhost`, the certificate's name, and any name in `BV_ALLOWED_HOSTS`); anything else gets `421`, which defeats DNS rebinding. Login and WebSocket requests that carry an `Origin` must match this server's scheme and host exactly. A session cookie is accepted on a WebSocket only together with an `Origin`; only the token header may go without one. A reverse proxy that terminates TLS is not supported: the browser's `Origin` would say `https` while the server sees `http`.
- Every page and every error, the `421` included, carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a Content-Security-Policy that confines the page to its own origin and forbids framing. The page is a single file, so the policy allows its inline script and style, and it allows `ws:` and `wss:` connections because the stream needs them. There is no HSTS on purpose: it would pin the whole host name, every port.
- Input is accepted only for the tab a client is viewing. Keys, text, mouse clicks and reloads are dropped for 0.8 s after a tab change the user did not ask for; pointer moves and the wheel are not.
- Before killing a managed browser the plugin checks that the registered pid still has the recorded start time and the expected `--user-data-dir`, so a recycled pid is never signalled.
- Purging a profile is limited to directories directly under the plugin's `profiles/`.

## Disclosure

What you should know before installing:

- **Network.** Starts an HTTP/WebSocket server (aiohttp) that listens on the explicit IPv4 addresses above. It speaks plain HTTP unless you enable HTTPS: on a Tailscale network WireGuard encrypts plain HTTP, on any other network it is not encrypted. The plugin makes no outbound network calls of its own and talks to Chromium over `http://127.0.0.1`, never through a proxy. The only exception is the opt-in `tls tailscale`, which runs `tailscale cert`: that contacts the Tailscale control plane and Let's Encrypt, and the machine's MagicDNS name appears in public Certificate Transparency logs. The certificate's private key is stored in a `0600` file in the plugin data directory.
- **Credentials.** Managed browsers keep a persistent profile on disk, including cookies and logins, under the plugin data dir. The master secret lives in the same directory. The server and Chromium start with a minimal environment (PATH, HOME, locale, display and proxy variables), not the environment of the Hermes process, so API keys held there are not inherited. Proxy settings, which can contain credentials, and the desktop-session variables that choose Chromium's password store are passed on, so that the browser reaches the web and an existing profile still opens.
- **Shell commands.** Launches Chromium (`--remote-debugging-port` on loopback, `--user-data-dir=<profile>`), runs `chromium --version` to pick a binary at least as recent as the one that created a profile, runs `tailscale ip -4` to find the Tailscale address, `tailscale status --json` and `tailscale cert` when HTTPS is enabled with `tls tailscale`, and runs `hermes config set|unset browser.cdp_url` when you use `use` or `stop`.
- **Global setting.** `use` writes `browser.cdp_url`, which makes every Hermes session of that profile (other chats, cron jobs) drive that browser until cleared, and `browser_exec` then acts with every login that browser holds. That is the point of `use`, and the tool description says it must only be called when the user asks. `stop` clears the setting first and stops the browser only if that worked. `hermes config` runs without an extra approval step, as a direct consequence of the action you asked for.
- **Background processes.** The streaming server is a detached process that outlives Hermes sessions and exits by itself after the idle timeout. Managed Chromium processes keep running until stopped.
- **Reads outside its own data.** `/proc/<pid>` and `/proc/net/tcp` on Linux, the same facts through `psutil` on macOS (process identity, and to check that a new browser holds its debug port), and the Chromium builds Hermes installed under `<hermes home>/tools/`.
- **Local CDP access.** Any CDP client on the same machine, Hermes included, can read what is in a page, password fields included. This plugin does not add that exposure, but it is why you should keep the host single-user.
- **Links in the tool result.** `start` and `link` return login links. They work once and expire after 10 minutes, so a transcript does not hold anything reusable once a link has been used or has expired.
- **Plain HTTP unless you enable HTTPS.** Without `tls`, the session cookie has no `Secure` flag and neither it nor the stream is encrypted on the wire. On a Tailscale network WireGuard encrypts everything; on any other network enable `tls` or do not expose the port.
- **What the agent sees.** `status` and `list` return, for each tab of the registered browsers, its origin (scheme and host, never path or query) and a title cut to 60 characters. They land in the session transcript.
- **Chromium's own traffic.** The browser is a regular, unmodified Chromium. Besides what the opened pages need it makes the background connections Chromium normally makes (the logs show, for example, Google's push messaging). The plugin neither adds nor hides any of it.
- **Telemetry.** None from the plugin.

## Known limitations

- HTTPS is opt-in, and the certificate is only refreshed when the server starts: a server kept running past the certificate's expiry keeps serving the old one (it exits after 60 idle minutes by default). If Tailscale cannot be reached at start, the certificate on disk is reused with a warning as long as it has not expired; the server never falls back to plain HTTP on a TLS address.
- No per-device session list: a session lasts until it expires or the master secret is rotated, and rotating signs every browser out.
- Frame rate and bandwidth were measured on loopback with simulated delay, not on real WAN links.
- On a phone in portrait the remote page (1280 px wide) is scaled to about a third, so fields are small. Landscape is much more comfortable.
- The tool handler is synchronous: a cold start can block for up to about 75 seconds (15 s for Chromium plus up to 60 s for the server), more if `hermes config` is slow (30 s) or `tls tailscale` has to ask Tailscale for a certificate (up to 130 s).
- Cookies are not scoped by port: on the same host name another service on a different port receives the viewer's cookie. Its name carries the port and the value is signed and `HttpOnly`, so it is useless to that service, but it is sent.
- After the 0.8 s quiet window, keystrokes go to the tab the agent opened. A click is not required first.
- A window that joins a browser's stream in the instant the stream is being closed reconnects by itself. A window that joins later is given the newest frame we hold, so a page that does not repaint is not shown blank, but that frame can be a moment old.
- With two windows on different tabs of a browser that has a real window (not headless), tab activation for streaming can alternate; headless browsers are not affected. Tab activations done by the agent itself are not tracked.
- `tls` keys must not be passphrase-protected, and on Linux `tailscale cert` needs root or your user set as Tailscale operator (`tailscale set --operator=$USER`, once, as root).
- Not tested on Windows clients with non-US keyboard layouts beyond AltGr handling.

## Development

```bash
python3 -m unittest -v tests.test_viewer      # needs aiohttp
hermes plugins validate . --install-deps      # the catalog gate
```

## License

MIT, see `LICENSE`.
