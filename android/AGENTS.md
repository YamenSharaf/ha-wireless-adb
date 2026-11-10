# Local WiFi Debug Sync

This project is a native Android (Kotlin) application designed for Google/Android TV devices. It autonomously enables Wireless Debugging, discovers the assigned random port, and reports it to a Home Assistant webhook — **headlessly**, primarily on device boot. See [Architecture](#architecture-headless-port-sync).

## Core Mandates
- **No Autonomous Sync:** Do NOT sync, push, or commit changes to GitHub unless specifically instructed by the user in the current turn.
- **No Autonomous Builds:** Do NOT run build commands (e.g., `./gradlew assembleDebug`, `gradle build`) unless specifically instructed by the user in the current turn.
- **Security & Secrets:** NEVER upload or commit real secrets (API tokens, private keys, production passwords) to GitHub; keep them in local, ignored files. The release signing keystore is **not** in the repo: CI gets it from repository secrets (see [CI / Build Pipeline](#ci--build-pipeline)). Never commit it. The HA webhook/device defaults in the workflow are LAN-only and fine to keep (they go away when the app stops reporting to a webhook).

## Objective
Enable a "one-click" synchronization of the Android Wireless Debugging port to Home Assistant, allowing the HA Android TV integration (ADB) to reconnect without manual intervention when the port changes.

## App Details
- **App Name:** Local WiFi Debug Sync
- **Package ID:** `com.enuff.localwifidebug`
- **Main Activity:** `.MainActivity`
- **Webhook Endpoint / Device ID:** compiled into `BuildConfig` from `WEBHOOK_URL` / `DEVICE_ID` in `local.properties` (CI defaults: webhook id `<webhook-id>`, `device_id` `<device-id>`). Build-time constants, not runtime config.

## Architecture (headless port sync)
The sync runs **headlessly** — no UI, no screen takeover. Two triggers feed one code path:
- **On boot (primary):** `BootReceiver` (`RECEIVE_BOOT_COMPLETED`) enqueues the sync. Wireless Debugging is off after a reboot and returns on a new random port, so the device **proactively pushes** the fresh port to HA rather than waiting to be launched.
- **Manual / recovery:** `MainActivity` is an invisible launcher — plain `Activity` with `Theme.Invisible` (`windowNoDisplay`; `noHistory` + `excludeFromRecents` + empty `taskAffinity`, no window animation). It shows no window, enqueues the same worker, and `finish()`es in `onCreate`. HA's `remote.turn_on` with `activity: localwifidebug://launch` (via the Android-TV **Remote** integration — independent of ADB) and `adb monkey LAUNCHER` both hit this. Use the app link, not the bare package name: the Remote integration turns a package name into `market://launch?id=<pkg>`, and since the Play Store update of ~2026-10-20 nothing on the Bravia resolves `market://`, so those launches fail silently (logcat: `AtvRemote.AppLinkFeature: Failed to launch app link`); launching also clears Android's "stopped state" so `BOOT_COMPLETED` will fire.

Both enqueue **`SyncWorker`** (WorkManager, **expedited**): enable WD (`WRITE_SECURE_SETTINGS`) → `NetworkType.CONNECTED` constraint → mDNS discover `_adb-tls-connect._tcp.` (8s timeout) → **single** POST to the HA webhook.
- **Own-service match (1.1.1):** discovery sees every device on the LAN with Wireless Debugging on, so a resolved service only counts if one of its addresses is one of this device's own interface addresses (`NetworkInterface`, no permission needed). Resolves are queued one at a time (older NsdManager stacks reject concurrent resolves). Before 1.1.1 the worker took the first service that resolved and reported a Samsung device's port (40501) after the 03:01 standby restarts on 2026-11-09/10; ADB Keeper's own mDNS lookup masked it.
- **Retry boundary:** "done" = webhook **HTTP 2xx only**. The worker has zero visibility into whether the add-on then reconnects over ADB, so it never retries on downstream state. Bounded to 3 attempts to deliver one POST past transient failures; no port discovered → **no POST invented** (the HA add-on's own ~60s mDNS watch is the backstop).
- **Feedback:** notifications only — **no toasts**. A high-importance `WRITE_SECURE_SETTINGS` self-check fires a notification with the exact `pm grant` command if the grant is missing (that grant does **not** survive a signature-changed reinstall and can't be UI-granted). On Android TV notifications may not render visibly; the real success signal is the webhook POST / logcat tags `WiFiDebugWorker`, `WiFiDebugActivity`. The OS's own "wireless debugging enabled" toast appears only when a recovery actually re-enables WD — unsuppressable and expected.
- **Expedited work** runs as a JobScheduler job on the API-34 target (no `foregroundServiceType` landmine); `getForegroundInfo` + `FOREGROUND_SERVICE` exist only as the API<31 fallback. `BootReceiver` stamps enqueue time; the worker logs enqueue→run latency + attempt so silent expedited-quota degradation is observable.

### Two-part system (coordinate before changing the contract)
This app is **one half** of a system. A separate **HA/add-on** side (an "ADB Keeper" add-on) consumes the webhook, connects over ADB, runs `adb tcpip <classic_port>` (5556 since 2026-11-01: Sony's webapi can grab 5555 at boot), and reloads the `androidtv` integration. The **contract is fixed**: `POST {HA}/api/webhook/<id>` with body `{ "port": <int>, "device_id": "<string>" }`. HA reads only `port`, but `device_id` must keep being sent (future multi-TV branching). Do **not** change the webhook id / port / URL / payload / add auth without coordinating the HA side in lockstep.

### Deferred (non-blocking)
- **Expedited-quota stress test** — parked (user doesn't rapid-reboot; `BOOT_COMPLETED` is a protected broadcast, so ADB can't fake reboots). If ever needed: add a DEBUG-only exported receiver (e.g. action `com.enuff.localwifidebug.DEBUG_ENQUEUE`) that enqueues the same worker, to fire rapidly over ADB and watch for the silent deferred-fallback.
- **Plex refresh-switch hitch** — not yet exercised under 24fps/48Hz DV playback; watch during normal use.

## Installation & Deployment (via ADB)

### 1. Build the APK
Navigate to the project directory and run:
```bash
export ANDROID_HOME=$HOME/android-sdk
export PATH=$HOME/gradle/gradle-8.1.1/bin:$PATH
# Ensure the ARM64 AAPT2 patch is applied to the Gradle cache if building on ARM64
./gradlew assembleDebug
```

### 2. Connect & Install
```bash
adb connect 192.168.50.10:5555
adb install /opt/usb/steven/projects/localwifidebug/app/build/outputs/apk/debug/app-debug.apk
```

### 3. Critical Permissions (Manual Grant Required)
`WRITE_SECURE_SETTINGS` must be granted over ADB (it cannot be UI-granted):
```bash
# Allow toggling system settings (Wireless Debugging)
adb shell pm grant com.enuff.localwifidebug android.permission.WRITE_SECURE_SETTINGS

# Allow showing notifications on Android 13+
adb shell pm grant com.enuff.localwifidebug android.permission.POST_NOTIFICATIONS
```

**Grant persistence:** thanks to the stable committed keystore, an **in-place update** (`adb install -r`, same signature) **keeps all grants** — no re-grant needed. Only a **signature change** (e.g. the historical throwaway-key builds → the stable key) forces uninstall + reinstall, which silently drops `WRITE_SECURE_SETTINGS`. The worker's self-check will surface that with a `pm grant` notification. Full one-time-per-device migration on a signature change:
```bash
adb uninstall com.enuff.localwifidebug
adb install app-release.apk
adb shell pm grant com.enuff.localwifidebug android.permission.WRITE_SECURE_SETTINGS
adb shell pm grant com.enuff.localwifidebug android.permission.POST_NOTIFICATIONS
# Clear Android's "stopped state" so BOOT_COMPLETED will fire:
adb shell monkey -p com.enuff.localwifidebug -c android.intent.category.LAUNCHER 1
```

## CI / Build Pipeline
Since 2026-11-10 the app lives in `android/` of the `ha-wireless-adb` repo (moved from `YamenSharaf/localwifidebug` with its history). GitHub Actions (`.github/workflows/android.yml` at the repo root, `working-directory: android`) builds and releases the APK. Key decisions:
- **Triggers:** push to `main` touching `android/**` (docs excluded), plus manual `workflow_dispatch`. Push builds use the hardcoded default webhook/HA-URL/device inputs (`<webhook-id>` / `<ha-host>:8123` / `<device-id>`); manual runs can override them. These get compiled into `BuildConfig` at build time.
- **No `gradlew`:** the project ships only the wrapper jar, so CI invokes it directly (`java -cp gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain assembleRelease`).
- **Versioning (hybrid):** `versionCode` = Actions run number **+ 100** (`VERSION_CODE_OFFSET`). Run numbers restarted at 1 in the new repo while the last localwifidebug build was versionCode 7, so the offset keeps every build an installable in-place upgrade. `versionName` is bumped intentionally by a human via `scripts/bump.sh [patch|minor|major]`.
- **Releases:** each build publishes a GitHub **prerelease** tagged `app-v<versionName>-<versionCode>` with the signed APK attached. Prerelease + `app-` prefix because HACS reads this repo's releases as integration versions and ignores prereleases by default.
- **Stable signing key, from secrets:** every APK is signed with the same key so updates install in place and the ADB-granted `WRITE_SECURE_SETTINGS` **survives updates instead of being wiped on every reinstall**. The keystore is NOT in the repo (it was filtered out of the imported history): CI restores it from the `ANDROID_KEYSTORE_BASE64` secret (password in `ANDROID_KEYSTORE_PASSWORD`, alias `localwifidebug`) and deletes it after the build. Certificate SHA-256 `6E:A4:6C:99:D9:A0:48:FC:11:49:02:6F:5C:0A:76:26:63:F5:6F:C4:89:08:45:D9:7E:FE:95:8C:1F:4B:76:1B`. If the signing key ever changes, devices need a one-time uninstall + reinstall + re-grant.

## Home Assistant Integration

### Script
Add this to your `scripts.yaml` to launch the app from HA:
```yaml
local_wifi_debug_sync:
  alias: "Local WiFi Debug Sync"
  description: "Launches the WiFi Debug app to report its random port to HA"
  sequence:
    - service: remote.turn_on
      target:
        entity_id: remote.steven_tv
      data:
        activity: "localwifidebug://launch"   # not the package name: market:// links no longer resolve
  mode: single
  icon: mdi:wifi-sync
```

### Automation Suggestion
The reported port can be used to update the `media_player` configuration or trigger an ADB reconnection service in Home Assistant.

## Technical Notes (ARM64 Build Environment)
Since this environment is ARM64, the standard Android SDK `aapt2` (x86_64) will fail.
- A native ARM64 `aapt2` binary is required at the project root: `localwifidebug/aapt2` (this binary is excluded from the git repository).
- During builds, this binary is patched into the Gradle cache: `~/.gradle/caches/transforms-3/.../transformed/aapt2-8.1.1-10154469-linux/aapt2`.
- `android.useAndroidX=true` and `android.enableJetifier=true` are required in `gradle.properties`.
- `android:usesCleartextTraffic="true"` is enabled in the manifest for local HTTP communication to Home Assistant.
