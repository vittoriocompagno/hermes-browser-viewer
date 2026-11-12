# browser-viewer

Live view and human takeover of the browser Hermes drives.

The plugin streams the tabs of a Chromium that Hermes controls to a web page you open from another device (phone, laptop). You see what the agent sees and you can click, type, scroll and paste yourself. Use it to log in, pass 2FA or a CAPTCHA, or just watch the agent work. When you are done, the agent keeps using the same logged-in browser.

- Several windows and several browsers at once, each browser with its own persistent profile (one per account).
- Follow mode: the page follows the tab the agent works on, and pins to the tab you are using while you type.
- Adaptive stream quality from the measured round-trip time of each client, or a fixed level.
- Mouse, keyboard (including AltGr), drag, paste, and touch (tap clicks, one-finger drag scrolls, a keyboard button opens the phone keyboard).
- No keystrokes, pasted text or token are written to any log.

Linux only: process identity checks read `/proc`.

## Install

```bash
hermes plugins install browser-viewer          # from the catalog, once listed
hermes plugins install vittoriocompagno/hermes-browser-viewer   # straight from GitHub
hermes plugins enable browser-viewer
```

Requires Hermes `>=0.23.1`, Python dependency `aiohttp>=3.9,<4` (installed with the plugin) and a Chromium or Chrome binary on the machine (`chromium`, `google-chrome-stable`, a path in `BV_CHROMIUM`, or one Hermes installed under its `tools/` directory).

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
hermes browser-viewer token --rotate   # new link, the old one stops working immediately
```

From a chat: the `browser_viewer` tool (actions `status`, `start`, `link`, `list`, `new`, `adopt`, `use`, `stop`, `rotate_token`) and the `/viewer` slash command. The agent starts a viewer, gives you the link, and after you have logged in switches `browser_exec` to that browser with `use`.

Open the link from the device you want to control the browser from. The server listens on the addresses you choose with `BROWSER_VIEWER_HOSTS`, by default loopback plus the machine's Tailscale IPv4 if `tailscale` is installed. It never binds `0.0.0.0`. It shuts itself down after 60 minutes with no open window.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `BROWSER_VIEWER_HOSTS` | comma-separated specific IPv4 addresses to listen on | `127.0.0.1` plus the Tailscale IPv4 |
| `BV_CHROMIUM` | path to the Chromium or Chrome binary | auto-detected |
| `BV_START_TIMEOUT` | seconds to wait for a new Chromium | `15` |
| `BV_CLOSE_GRACE` | seconds an empty hub keeps its CDP connection | `20` |
| `BROWSER_VIEWER_HOME` | override of the data directory (the plugin sets it for its own server process; useful for tests) | the plugin data dir of the active profile |

State lives in the plugin data directory (`<hermes home>/plugin-data/browser-viewer/`): browser registry, profiles, token, logs.

## Security model

- Whoever holds the link controls a browser that may be logged in to your accounts. Treat the link like a password.
- The token is random, stored in a `0600` file inside a `0700` directory, required for the WebSocket and the stats endpoint, and rotated without restarting the server. The static page is served without it.
- The page moves the token from the address bar into `sessionStorage` right after loading.
- The CDP port of every browser listens on `127.0.0.1` only. Adopted browsers must be loopback endpoints, and the plugin never kills a browser it did not start.
- Cross-origin WebSocket connections are refused (`Origin` must match `Host`).
- Input is accepted only for the tab a client is viewing, and is dropped for 0.8 s after a tab change the user did not ask for.
- Before killing a managed browser the plugin checks that the registered pid still has the recorded start time and the expected `--user-data-dir`, so a recycled pid is never signalled.
- Purging a profile is limited to directories directly under the plugin's `profiles/`.

## Disclosure

What you should know before installing:

- **Network.** Starts an HTTP/WebSocket server (aiohttp) that listens on the explicit IPv4 addresses above. The link is plain HTTP: on a Tailscale network WireGuard encrypts it, on any other network it is not encrypted. The plugin makes no outbound network calls. It talks to Chromium over `http://127.0.0.1`.
- **Credentials.** Managed browsers keep a persistent profile on disk, including cookies and logins, under the plugin data dir. The token file lives in the same directory.
- **Shell commands.** Launches Chromium (`--remote-debugging-port` on loopback, `--user-data-dir=<profile>`), runs `chromium --version` to pick a binary at least as recent as the one that created a profile, runs `tailscale ip -4` to find the Tailscale address, and runs `hermes config set|unset browser.cdp_url` when you use `use` or `stop`.
- **Global setting.** `use` writes `browser.cdp_url`, which makes every Hermes session of that profile (other chats, cron jobs) drive that browser until cleared. `stop` clears it if it points at the stopped browser.
- **Background processes.** The streaming server is a detached process that outlives Hermes sessions and exits by itself after the idle timeout. Managed Chromium processes keep running until stopped.
- **Reads outside its own data.** `/proc/<pid>` for process identity, and the Chromium builds Hermes installed under `<hermes home>/tools/`.
- **Local CDP access.** Any CDP client on the same machine, Hermes included, can read what is in a page, password fields included. This plugin does not add that exposure, but it is why you should keep the host single-user.
- **Token in the tool result.** The link, token included, is returned by the `start` and `link` actions, so it appears in the session transcript. Rotate it with `rotate_token` if a transcript is shared.
- **Telemetry.** None.

## Known limitations

- The token is long-lived and travels in the link. A one-time bootstrap token exchanged for an `HttpOnly` cookie would be stronger and is not implemented.
- Frame rate and bandwidth were measured on loopback with simulated delay, not on real WAN links.
- On a phone in portrait the remote page (1280 px wide) is scaled to about a third, so fields are small. Landscape is much more comfortable.
- The tool handler is synchronous: a cold start can block for up to about 75 seconds (15 s for Chromium plus up to 60 s for the server).
- Not tested on Windows clients with non-US keyboard layouts beyond AltGr handling.

## Development

```bash
python3 -m unittest -v tests.test_viewer      # needs aiohttp
hermes plugins validate . --install-deps      # the catalog gate
```

## License

MIT, see `LICENSE`.
