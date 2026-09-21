# LG webOS TV: alternatives investigation

Date: 2026-09-21. Investigation only, no code changes.

Questions asked:

1. What is the most powerful command-line tool or SDK to control LG webOS TVs over the local network?
2. Can it jump to, indicate, or change a Netflix episode?

## 1. Current setup

`devices/tv/` drives the TV with a bundled, statically linked Go CLI, `devices/tv/tools/lg-webos-ssap`
(source outside this repo at `~/projs/gh/lg-webos-ssap`). It speaks LG's SSAP protocol
(JSON over WebSocket) to `ws://192.168.2.237:3000`.

| Script | What it does today |
|---|---|
| `block.sh` | `lg-webos-ssap -cmd turn-off` |
| `unblock.sh` | no-op (`exit 0`) |
| `getState.sh` | ICMP ping, `up`/`down` |

The CLI already supports much more than the driver uses: `info`, `toast`, `list-apps`, `launch`
(with `-payload`), `close`, volume, channels, inputs, and `play`/`pause`/`stop`/`rewind`/`fast-forward`.
It has no power-on and no remote-button emulation.

Facts read from the TV on 2026-09-21 with the paired CLI (`-cmd info`):

| Item | Value |
|---|---|
| Model | 49LH590V-ZD (2016 LH series, webOS 3.0 generation) |
| Transport | plain `ws://` on port 3000 works, no `wss://` needed |
| Features | `dvr: true`, `3d: false`, receiver `dvb` |

Exact firmware version was not queried. bscpylgtv `get_software_info` or the SSAP endpoint
`com.webos.service.update/getCurrentSWInformation` would return it.

## 2. Security finding: pairing key committed to git

`devices/tv/.lg-webos-ssap.key` and an unused duplicate `devices/tv/tools/.lg-webos-ssap.key`
are tracked in git, and the repo remote is public (`github.com/zipizap/kidsout`). The key grants
full remote control of the TV to anyone on the LAN. Treat it as compromised. The Xbox design doc
already lists this as item S2-02, deferred.

Recommended follow-up (separate task):

1. Re-pair with `lg-webos-ssap -cmd initialize-key`. The TV issues a new key and the old one stops working.
2. `git rm --cached` both key files, add them to `.gitignore`.
3. Delete the unused duplicate under `tools/`.
4. Optionally purge the old key from git history. After rotation the leaked value is dead, so this is cosmetic.

## 3. Most powerful local CLI / SDK

Every open-source tool speaks the same protocol as the current Go CLI: SSAP over WebSocket on
`ws:3000` (older firmware) or `wss:3001` (firmware from about 2023 on). None of them can do more
than SSAP permits unless the TV is rooted. Ranked by capability:

### 3.1 bscpylgtv (Python library + `bscpylgtvcommand` CLI). Most capable unrooted option

- Repo: https://github.com/chros73/bscpylgtv. Python >= 3.8. `pip install bscpylgtv` (lite, no calibration).
- Everything the current CLI has, plus:
  - Remote-button emulation over the separate input socket: `button OK`, `button LEFT`, `button EXIT`, etc.
  - Pointer move / click / scroll.
  - `get_current_app`, `get_apps_all`, `launch_app_with_params`, `close_app`.
  - `take_screenshot`, `get_software_info`, `get_system_info`, `send_message` (toast).
  - `turn_screen_off` / `turn_screen_on`, `power_off`, `reboot`.
  - Subscriptions to volume, mute, current app, power state, etc.
  - `luna_request` / `set_settings`: calls Luna bus endpoints such as
    `com.webos.settingsservice/setSystemSettings` from an unrooted TV via the createAlert trick.
    This is what unlocks picture, sound, and hidden system settings that SSAP does not expose.
- CLI chains commands with sleeps:
  `bscpylgtvcommand -p /path/key.sqlite 192.168.2.237 button INFO , sleep 1 , get_current_app`.
- Downsides: Python dependency on the kidsout host; calibration features irrelevant here;
  a webOS 26 hang in `luna_request` was fixed in a fork PR, irrelevant for webOS 3.x.

### 3.2 aiowebostv (Python library, Home Assistant's). Best maintained

- Repo: https://github.com/home-assistant-libs/aiowebostv. Python >= 3.11.
- Full SSAP surface: `launch_app`, `launch_app_with_params`, `launch_app_with_content_id`,
  `get_current_app`, `get_media_foreground_app` (`com.webos.media/getForegroundAppInfo`),
  `get_power_state`, `button()`, media controls, subscriptions.
- `connect()` tries `ws:3000` and falls back to `wss:3001` automatically.
- `power_on()` is an SSAP call to `system/turnOn`. It only works while the TV's socket is alive
  (networked standby). Real power-on from off state needs Wake-on-LAN.
- No CLI shipped. A short Python wrapper would be needed. No Luna trick.

### 3.3 lgtvremote-cli (Python CLI, command `lgtv`). Most convenient CLI

- Repo: https://github.com/griches/lgtvremote-cli. `pip install lgtvremote-cli`.
- Power on via Wake-on-LAN (stores the TV MAC), `lgtv off`, `lgtv power`.
- Apps by name (`lgtv app netflix`), inputs by number or alias, nav keys, media, volume.
- Luna picture and sound settings via the same alert workaround as bscpylgtv.
- `lgtv self-test` sweeps every safe command and reports which ones the TV actually answers.
  Useful to learn what this 2016 firmware supports.
- Targets newer wss/PIN firmware too.

### 3.4 lgtv2 (Node.js)

- Repo: https://github.com/hobbyquaker/lgtv2. Reference implementation, same SSAP surface,
  ws/wss autodetect since 1.7. Only relevant if a Node stack is preferred.

### 3.5 Beyond SSAP: root the TV (webosbrew Homebrew Channel). Most powerful, invasive

- Root gives SSH and `luna-send` on the private Luna bus: subscribe to
  `com.webos.applicationManager/getForegroundAppInfo`, query `com.webos.media` pipelines,
  write settings, install background services on the TV itself (this is how third-party
  parental-control apps like TV Guardian work).
- Feasibility on this 2016 webOS 3.x set is uncertain. RootMyTV targets webOS 4 to 6 on firmware
  before mid-2022. GetMeIn was reported working on webOS 2.2 and 3.4.2 on Realtek SoCs only.
  Exact firmware and SoC would have to be checked first.
- LG's official Developer Mode app (SSH on port 9922 through the `ares-*` CLI, no root, 1000-hour
  limit) is a lighter path to install a custom webOS app that can call public Luna APIs.
- webOS also exposes DIAL over HTTP (`/apps/Netflix`, `/apps/YouTube`) without pairing.
  Launch and stop only, no state reading.

### 3.6 Recommendation

Keep the current Go CLI for `turn-off`. It works and has no runtime dependencies.
If richer control is wanted, adopt bscpylgtv (most capability in one CLI) or lgtvremote-cli
(nicest CLI, Wake-on-LAN power-on). Do not root a family TV for this project.

## 4. Netflix episodes

### 4.1 Jump to a specific title or episode: yes, with caveats

SSAP `system.launcher/launch` accepts a `contentId` that is passed to the Netflix app as its launch
parameter. Netflix expects a query string with `m=<videoId>`. The Home Assistant community example
for webOS:

```yaml
command: system.launcher/launch
payload:
  id: netflix
  contentId: "m=https://www.netflix.com/watch/70287700"
```

The existing Go CLI can already send this:

```sh
./lg-webos-ssap -addr 192.168.2.237:3000 -key-file ../.lg-webos-ssap.key \
  -cmd launch -arg netflix -payload '{"contentId":"m=https://www.netflix.com/watch/<episode-id>"}'
```

Netflix IDs come from the browser URL. A series or movie ID (`netflix.com/title/<id>`) opens
the title page. An episode ID (`netflix.com/watch/<id>`) targets that episode. Reports differ on
whether playback autostarts or lands on the title page. The 2016 Netflix app build may accept
only a bare numeric `m=<id>` rather than the URL form. This needs a live test on this TV; it was
not run during the investigation because it would take over the screen.

### 4.2 Indicate which episode is playing: no, not via any local API

- LG staff on the webOS developer forum: "the webOS TV does not provide 3rd party apps' information."
- SSAP exposes only the foreground app id (`netflix`) and, on newer webOS versions,
  `com.webos.media/getForegroundAppInfo` with `playState` (`playing` / `paused` / `idle`) and an
  opaque `mediaId`. No title, season, or episode. Home Assistant saw a 404 on that endpoint even
  on webOS 6, so on webOS 3.x it very likely does not exist.
- Rooting does not help. Netflix keeps its state inside the DRM-protected app, not on the Luna bus.
  The only speculative hack is `take_screenshot` of the pause overlay, which is usually black for
  protected content.
- The only real source of "which episode" is the Netflix account's viewing activity page.
  Netflix has no public API for it, and it is cloud, not LAN. Out of scope for a local approach.

### 4.3 Next or previous episode: only by faking remote keys

The SSAP `media.controls` set is play, pause, stop, rewind, fast-forward. There is no
"next episode" command. It can be imitated by sending remote buttons through the input socket
(bscpylgtv or aiowebostv `button()`), for example OK then RIGHT then ENTER to hit Netflix's
on-screen "Next episode" button. That is UI-dependent and brittle. The alternative is a deep link
to the next episode ID as in 4.1, if the ID is known.

## 5. Useful upgrades for kidsout (not requested, for reference)

All of these are possible with the CLI already shipped, or with bscpylgtv.

- `getState.sh`: replace ping with `com.webos.applicationManager/getForegroundAppInfo`
  (bscpylgtv `get_current_app`) so `up` means "an app is in the foreground", and optionally count
  time only when streaming apps (Netflix, YouTube, Disney) are open.
- Warning before block: `system.notifications/createToast` ("5 minutes left"), already available
  as `lg-webos-ssap -cmd toast -arg "..."`.
- Softer block: `system.launcher/close` of the streaming app, or
  `com.webos.service.tvpower/power/turnOffScreen`, instead of full power-off.
- Power-on for unblock: Wake-on-LAN with the TV setting "Mobile TV On" / "Turn on via Wi-Fi"
  enabled. lgtvremote-cli implements it.
- Hardcoded addresses: `block.sh` and `getState.sh` use `192.168.2.237` inline while the CLI's
  compiled default is `192.168.1.237`. A small `device.json` like the Xbox driver has would avoid drift.

## 6. Verification steps for any follow-up

1. Netflix deep link: send `contentId` as bare `m=<id>` and as the URL form; observe title page vs autoplay.
2. Media state: `bscpylgtvcommand ... get_current_app`, then a raw request to
   `ssap://com.webos.media/getForegroundAppInfo` to see if webOS 3.x answers.
3. Firmware: `get_software_info` to pin the exact webOS version before considering rooting.
4. `lgtv self-test` from lgtvremote-cli to list which commands this firmware supports.

## 7. Sources

- bscpylgtv: https://github.com/chros73/bscpylgtv
- aiowebostv: https://github.com/home-assistant-libs/aiowebostv (`endpoints.py`, `webos_client.py`)
- lgtvremote-cli: https://github.com/griches/lgtvremote-cli
- lgtv2: https://github.com/hobbyquaker/lgtv2 and issue 25 (Netflix contentId)
- LG forum, "API endpoint/way to get what's playing": https://forum.webostv.developer.lge.com/t/api-endpoint-way-to-get-whats-playing/3789
- Home Assistant issue 91709 and PR 113774 (media playState via `com.webos.media/getForegroundAppInfo`)
- Home Assistant community, Netflix automation thread: https://community.home-assistant.io/t/has-anyone-figured-out-how-to-automate-playing-something-on-netflix-e-g-on-a-samsung-smart-tv/498399
- Home Assistant `webostv.command` docs: https://www.home-assistant.io/actions/webostv.command/
- webosbrew Luna service bus: https://www.webosbrew.org/pages/luna-service-bus
- webosbrew hidden system settings: https://www.webosbrew.org/pages/hidden-system-settings
- webosbrew Dev Mode: https://www.webosbrew.org/devmode/
- RootMyTV: https://github.com/RootMyTV/RootMyTV.github.io
- GetMeIn (XDA): https://xdaforums.com/t/getmein-one-time-rooting-jailbreaking-tool-for-webos-lg-tvs.3887904/
- openlgtv webOS hacking notes: https://gist.github.com/Informatic/1983f2e501444cf1cbd182e50820d6c1
- TV Guardian (webOS parental-control app): https://www.tv-guardian.com/
