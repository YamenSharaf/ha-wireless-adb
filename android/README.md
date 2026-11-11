# Wireless ADB Helper

A tiny, headless Android TV app that keeps **wireless debugging** turned on. It
is the device-side half of the [Wireless ADB](../README.md) Home Assistant
integration.

## Why it exists

Android turns wireless debugging off whenever the device restarts. Without it,
Home Assistant can't reach the TV over ADB, and only something running *on* the
TV can turn it back on (it takes the `WRITE_SECURE_SETTINGS` permission, which
can only be granted over ADB).

The helper does exactly that:

- **At boot**, it turns wireless debugging back on.
- **When launched** (from the TV, over ADB, or from Home Assistant through the
  [Android TV Remote](https://www.home-assistant.io/integrations/androidtv_remote/)
  integration with the link `wirelessadb://launch`), it does the same.
- **Every 15 minutes** a watchdog checks the setting and turns it back on if
  something switched it off. To turn wireless debugging off for good, disable or
  uninstall the helper first.

It shows no window and leaves nothing in recents. Finding the (random,
rotating) wireless-debugging port is the integration's job: the TV advertises it
over mDNS, and Home Assistant picks it up.

## Install

> The Wireless ADB integration will soon install and update the helper for you.
> Until then, install it by hand once:

1. Download the latest `app-release.apk` from this repository's releases (tags
   `app-v…`).
2. With ADB connected to the TV:

   ```bash
   adb install -r app-release.apk
   adb shell pm grant dev.yamen.wirelessadb android.permission.WRITE_SECURE_SETTINGS
   adb shell pm grant dev.yamen.wirelessadb android.permission.POST_NOTIFICATIONS
   # Launch once: Android doesn't deliver BOOT_COMPLETED to an app that has never run.
   adb shell am start -a android.intent.action.VIEW -d wirelessadb://launch
   ```

Updates signed with the same key install in place and keep the permissions.
A reinstall after an uninstall needs the `pm grant` lines again; the app shows a
notification with the exact command if the permission is missing.

### Permissions

| Permission | Why |
|---|---|
| `WRITE_SECURE_SETTINGS` | Turn wireless debugging on (`adb_wifi_enabled`). Can only be granted over ADB. |
| `RECEIVE_BOOT_COMPLETED` | Run at boot. |
| `POST_NOTIFICATIONS` | Tell you when the permission above is missing. |
| `INTERNET`, `ACCESS_NETWORK_STATE` | Only used for the optional webhook. |

## Optional: report the port to a webhook

Not needed with the Wireless ADB integration. For setups that still want a push
(for example an ADB-server add-on that switches the TV to classic ADB), the
helper can POST `{"port": <int>, "device_id": "<string>"}` to a URL after it
turns wireless debugging on. It's off by default and set over ADB:

```bash
adb shell am broadcast --include-stopped-packages \
  -a dev.yamen.wirelessadb.CONFIGURE -n dev.yamen.wirelessadb/.ConfigReceiver \
  --es webhook_url http://homeassistant.local:8123/api/webhook/<id> \
  --es device_id living_room_tv
```

Run the same command without `--es …` to show the current setting, or with
`--ez clear true` to turn the webhook off. Only senders holding
`WRITE_SECURE_SETTINGS` (the ADB shell, the system) can change it.

## Build

Requirements: JDK 17 and the Android SDK (platform 34). The project ships the
Gradle wrapper jar but no `gradlew` script, so call the wrapper directly:

```bash
java -cp gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain assembleRelease
```

Without a release key the build is signed with the Android debug key. Such an
APK can't update an installed release build (different signature).

Release builds come from CI (`.github/workflows/android.yml`). It signs with the
key in the repository secrets `ANDROID_KEYSTORE_BASE64` and
`ANDROID_KEYSTORE_PASSWORD` and publishes a prerelease tagged
`app-v<versionName>-<versionCode>`. Forks without those secrets get a
debug-signed APK as a build artifact and no release.

## History

Based on [stevene1919/localwifidebug](https://github.com/stevene1919/localwifidebug)
(MIT). Until version 1.1.1 the app was `com.enuff.localwifidebug` ("Local WiFi
Debug Sync"). Since 2.0.0 it is `dev.yamen.wirelessadb`, signed with a different
key, so updating from the old app means uninstalling it first. The old
`localwifidebug://launch` link still works.

## License

MIT, see [LICENSE](LICENSE).
