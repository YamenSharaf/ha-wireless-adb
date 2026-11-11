# Wireless ADB Helper: notes for contributors and coding agents

Native Android (Kotlin) app for Android TV / Google TV. It keeps **wireless
debugging** on, headlessly, so the Wireless ADB Home Assistant integration (in
`../custom_components/wireless_adb`) can reach the device. User-facing docs are
in [README.md](README.md).

## Mandates

- **Don't push, tag or release** unless the maintainer asked for it in the
  current conversation.
- **Don't run builds** unless asked. CI builds every push that touches
  `android/`.
- **Never commit signing material.** The release keystore lives only in the
  repository secrets (see CI below). `*.keystore` is git-ignored.
- **Keep one APK for every device.** Nothing device- or network-specific may be
  compiled in; per-device settings are runtime settings set over ADB.

## App facts

- Package / namespace: `dev.yamen.wirelessadb`; label "Wireless ADB Helper".
- `minSdk 30` (wireless debugging needs Android 11), `targetSdk 34`.
- Release builds are shrunk with R8 (`minifyEnabled`, `shrinkResources`); the
  app has no UI, so there's no AppCompat/Material dependency.
- Entry points:
  - `BootReceiver` (`BOOT_COMPLETED`): enqueues the sync at boot.
  - `MainActivity`: invisible launcher (`Theme.Invisible` with
    `windowNoDisplay`, `noHistory`, `excludeFromRecents`, empty
    `taskAffinity`). Enqueues the sync and `finish()`es in `onCreate`. Reached
    from the launcher, `adb shell monkey`, and the app links
    `wirelessadb://launch` and (legacy) `localwifidebug://launch`.
  - `ConfigReceiver`: optional webhook setting, see below.
  - `WatchdogWorker`: periodic (15 min, WorkManager minimum) unique work,
    scheduled from boot and every launch (`ExistingPeriodicWorkPolicy.UPDATE`).
    Turns `adb_wifi_enabled` back on if it's off; with a webhook configured it
    enqueues `SyncWorker` instead so the new port is reported.
- Logcat tags: `WiFiDebugWorker`, `WiFiDebugActivity`, `WiFiDebugBoot`,
  `WirelessAdbConfig`. Some vendor builds (e.g. Sony Bravia, Android 14) drop
  third-party app logs, so don't rely on logcat to confirm behaviour.

## How the sync works

`SyncWorker` (WorkManager, **expedited**, unique work, `REPLACE`):

1. Self-check `WRITE_SECURE_SETTINGS`. If missing, post a high-importance
   notification with the exact `pm grant` command and fail. The grant can't be
   given from the UI and doesn't survive an uninstall/reinstall, so a silent
   failure here would leave the app inert.
2. Set `Settings.Global.adb_wifi_enabled = 1` if it's off, then wait briefly
   for adbd to start.
3. **Only if a webhook is configured:** find this device's own
   `_adb-tls-connect._tcp` port over mDNS and POST it once.

Without a webhook the job ends after step 2. The integration finds the port by
itself.

Details that matter:

- **Own-service match.** mDNS discovery returns every device on the LAN with
  wireless debugging on (phones, other TVs). A resolved service only counts if
  one of its addresses is one of this device's own interface addresses. Resolves
  run one at a time: older `NsdManager` stacks reject concurrent resolves.
- **Retry boundary.** "Done" means the webhook returned HTTP 2xx. The worker
  can't see what happens downstream and never retries on it. At most 3 attempts;
  no port found means no POST.
- **Expedited work** runs as a JobScheduler job on API 31+. `getForegroundInfo`
  and `FOREGROUND_SERVICE` exist only for the API < 31 fallback.
  `BootReceiver` stamps the enqueue time so a deferred (out-of-quota) run shows
  up as latency in the log.
- **No toasts, no UI.** Android itself shows a "wireless debugging enabled"
  toast when the setting flips on; that's expected and can't be suppressed.

### Launching from Home Assistant

Use the Android TV Remote integration's `remote.turn_on` with
`activity: wirelessadb://launch`. Don't pass the package name: the Remote
integration turns a bare package name into `market://launch?id=<pkg>`, which
fails silently on devices where nothing resolves `market://` (seen on a Sony
Bravia after a Play Store update in October 2026).

## Optional webhook

Off by default. `WebhookConfig` stores `url` and `device_id` (default
`Build.MODEL`) in app-private SharedPreferences. `ConfigReceiver` sets, shows
or clears them:

```bash
adb shell am broadcast --include-stopped-packages \
  -a dev.yamen.wirelessadb.CONFIGURE -n dev.yamen.wirelessadb/.ConfigReceiver \
  --es webhook_url <url> --es device_id <name>   # or no extras / --ez clear true
```

The receiver is exported but declares
`android:permission="android.permission.WRITE_SECURE_SETTINGS"`, so only the
ADB shell and the system can send it; no app on the device can redirect where
the port goes.

Payload contract (keep it stable while anyone depends on it):
`POST <url>` with `{"port": <int>, "device_id": "<string>"}`.

The webhook only exists for setups that switch the device to classic ADB
through an external ADB server. Remove it once nothing depends on it.

## Signing and updates

Android identifies an app by package name **and** signing certificate. Updates
signed with the same key install in place and keep ADB-granted permissions; a
different key means uninstall, reinstall and re-grant.

- Release key: 4096-bit RSA, `CN=Yamen Sharaf, OU=yamen.dev, O=yamen.dev,
  L=Berlin, ST=Berlin, C=DE`, alias `wirelessadb`, certificate SHA-256
  `50:3A:B6:5A:9A:A9:14:D6:EC:2C:35:4C:E2:6A:D5:9E:BD:77:E4:F1:E1:4C:62:BF:75:CE:8D:8D:5F:40:EB:7C`.
- Builds without `app/release.keystore` fall back to the Android debug key.
- Before 2.0.0 the app was `com.enuff.localwifidebug` with another key. Moving
  a device over is a one-time uninstall of the old package, install, `pm grant`,
  and one launch.

## CI

`.github/workflows/android.yml` (repo root, `working-directory: android`):

- Runs on pushes to `main` that touch `android/**` (Markdown excluded) and on
  manual dispatch.
- The project has no `gradlew` script, only the wrapper jar:
  `java -cp gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain assembleRelease`.
- `versionCode` = run number + 100 (`VERSION_CODE_OFFSET`; never lower it,
  later builds would look like downgrades). `versionName` is bumped by hand with
  `scripts/bump.sh [patch|minor|major]`.
- With the secrets `ANDROID_KEYSTORE_BASE64` and `ANDROID_KEYSTORE_PASSWORD`
  present, the APK is release-signed and published as a **prerelease** tagged
  `app-v<versionName>-<versionCode>`. Prerelease, because HACS reads this
  repo's releases as integration versions and skips prereleases. The keystore
  is deleted after the build.
- Without the secrets (forks), the build is debug-signed and only uploaded as a
  workflow artifact.

## Building on ARM64

The SDK's `aapt2` is x86_64 only. On an ARM64 host, put a native `aapt2` at the
project root (git-ignored) and point Gradle at it, e.g. with
`android.aapt2FromMavenOverride=<path>` in `gradle.properties` of your local
setup. Prebuilt binaries: [ReVanced/aapt2](https://github.com/ReVanced/aapt2/releases).
