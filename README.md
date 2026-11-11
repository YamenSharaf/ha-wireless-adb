# Wireless ADB for Home Assistant

A custom integration that controls an Android TV over **wireless debugging**: the
pairing-code, TLS-encrypted ADB that Android 11+ uses instead of classic
`adb tcpip 5555`.

Home Assistant's core [Android Debug Bridge](https://www.home-assistant.io/integrations/androidtv/)
integration only speaks classic ADB. On TVs whose firmware no longer keeps
classic ADB across reboots (for example Sony Bravia on Android 14), that means
an external ADB server, a helper to flip the TV back to classic mode, and
automations to glue it together. This integration talks to wireless debugging
directly, so none of that is needed on the Home Assistant side.

> **Status: early development (0.1.0).** It runs on one Sony Bravia 7. Expect
> rough edges, and expect breaking changes before 1.0.

## How it works

- **Pairing.** The integration generates its own ADB key and pairs it with the
  TV using the six-digit code from *Wireless debugging > Pair device with
  pairing code*. The TV remembers the pairing.
- **TLS sessions.** It connects to the TV's wireless-debugging port over TLS
  (via [`androidtv-wifi`](https://pypi.org/project/androidtv-wifi/) /
  [`adb-shell-wifi`](https://pypi.org/project/adb-shell-wifi/)). No adb binary,
  no ADB server, no classic-mode flip.
- **Rotating port.** The wireless-debugging port changes whenever adbd restarts.
  The TV advertises it over mDNS as `_adb-tls-connect._tcp` with its serial in
  the name. Home Assistant's zeroconf discovery picks up the new port and the
  integration reloads; if a connect fails, the integration also looks the TV up
  itself. Matching is always by serial, never "first service found", because
  phones on the same network advertise the same service.

## What you get

The same entities and services as the core integration, forked from
Home Assistant 2026.11.0:

- `media_player` (state, current app, sources, volume, screen capture as artwork)
- `remote` (send key and shell commands)
- Services `wireless_adb.adb_command`, `download`, `upload`, `learn_sendevent`
- Options for app names, state detection rules, screencap interval and
  turn on/off commands

The integration's entities attach to the same device as the TV's other
integrations (Android TV Remote, Bravia), matched by MAC address.

## Requirements

- Home Assistant 2026.11 or newer
- An Android TV / Google TV device on Android 11+ with *Developer options >
  Wireless debugging* turned on
- **Wireless debugging must be on after every reboot.** Android turns it off
  when the device restarts. The [Wireless ADB Helper](android/) app turns it back
  on at boot; install it once (see its README). The integration will soon
  install and update it for you.

## Installation

Through the Marketplace (HACS) as a custom repository:

1. *Marketplace > ⋮ > Custom repositories*
2. Repository: `https://github.com/YamenSharaf/ha-wireless-adb`, type
   **Integration**, then **Add**.
3. Open **Wireless ADB**, download it, and restart Home Assistant.

Or copy `custom_components/wireless_adb` into your Home Assistant
`config/custom_components/` folder by hand and restart.

## Releases

A release is published automatically when the `version` in
`custom_components/wireless_adb/manifest.json` changes on `main`
(`.github/workflows/integration.yml`, after hassfest and HACS validation pass).
Versions ending in `a`, `b`, `rc` or `dev` plus a number (e.g. `0.2.0b1`) are
published as prereleases. The helper app's builds are separate prereleases
tagged `app-v…`.

## Setup

1. On the TV: *Settings > System > Developer options > Wireless debugging*: on.
2. In Home Assistant: *Settings > Devices & services > Add integration >
   Wireless ADB*, or accept the discovered device.
3. On the TV: *Wireless debugging > Pair device with pairing code*. Enter the
   **port** (after the colon) and the **code** it shows, and submit while the
   dialog is still open.

If this Home Assistant is already paired with the TV (for example after you
deleted and re-added the entry), leave the code empty.

## Differences from the core integration

- Wireless debugging (TLS) only. No classic ADB, no ADB server, no Fire TV.
- Unique ID is the device serial (core uses the MAC address).
- Commands get `2>/dev/null` after every piped `dumpsys`. Some firmwares print
  `Failed to write while dumping service …: Broken pipe` to stderr when a
  `grep -q` closes the pipe early; `adb shell` mixes that into the output and
  the TV reads as off. Upstream python-androidtv has the same issue.
- The TLS context is built once per key, outside the event loop, instead of on
  every connect.

## Known limitations

- Screen capture still runs `screencap -p` on the TV, which takes a couple of
  seconds of TV CPU per capture.
- Losing the pairing (rare, but it happens on some firmwares) currently shows
  up as the device being unavailable. A repair flow that asks for a new code is
  planned.

## Credits and license

Forked from Home Assistant's `androidtv` integration (Apache License 2.0,
Copyright Home Assistant contributors). The first commit in this repository is
the unmodified upstream code, so every change is visible as a diff. Built on
[`androidtv-wifi`](https://pypi.org/project/androidtv-wifi/) and
[`adb-shell-wifi`](https://pypi.org/project/adb-shell-wifi/) by Jason Novak,
which are forks of Jeff Irion's `python-androidtv` and `adb_shell`.

The TV helper app in [`android/`](android/) is based on
[stevene1919/localwifidebug](https://github.com/stevene1919/localwifidebug) and
is licensed under MIT ([android/LICENSE](android/LICENSE)).

The integration is licensed under the Apache License 2.0; see [LICENSE](LICENSE).

## Repository layout

- `custom_components/wireless_adb/`: the Home Assistant integration
- `android/`: the TV helper app that turns wireless debugging back on at boot
  (Kotlin, MIT; built by `.github/workflows/android.yml` and published as
  `app-v…` prereleases)
- `assets/`: source artwork
