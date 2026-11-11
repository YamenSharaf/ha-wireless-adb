package dev.yamen.wirelessadb

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

/**
 * Optional webhook that receives the wireless-debugging port after each sync.
 *
 * Off by default: with the Wireless ADB integration, Home Assistant finds the
 * port itself over mDNS, so the app only has to turn wireless debugging on.
 * The webhook exists for setups that still need a push (e.g. an ADB server
 * add-on that flips the device to classic ADB).
 *
 * Stored in app-private SharedPreferences, set over ADB through [ConfigReceiver].
 */
data class WebhookConfig(val url: String?, val deviceId: String) {
    val enabled: Boolean get() = !url.isNullOrBlank()

    companion object {
        private const val PREFS = "webhook"
        private const val KEY_URL = "url"
        private const val KEY_DEVICE_ID = "device_id"

        fun load(context: Context): WebhookConfig {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            return WebhookConfig(
                url = prefs.getString(KEY_URL, null),
                deviceId = prefs.getString(KEY_DEVICE_ID, null) ?: Build.MODEL,
            )
        }

        /** Returns false if [url] is set but isn't an http(s) URL. */
        fun save(context: Context, url: String?, deviceId: String?): Boolean {
            if (url != null && url.isNotBlank() &&
                !(url.startsWith("http://") || url.startsWith("https://"))
            ) {
                return false
            }
            val editor = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            url?.let { if (it.isBlank()) editor.remove(KEY_URL) else editor.putString(KEY_URL, it) }
            deviceId?.let {
                if (it.isBlank()) editor.remove(KEY_DEVICE_ID) else editor.putString(KEY_DEVICE_ID, it)
            }
            return editor.commit()
        }

        fun clear(context: Context): Boolean =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().commit()
    }
}

/**
 * Sets the optional webhook over ADB. Exported, but the manifest requires the
 * sender to hold WRITE_SECURE_SETTINGS, which the ADB shell has and ordinary
 * apps can't get, so only ADB (or the system) can change where the port goes.
 *
 * ```
 * # set (both extras optional; an empty string removes that value)
 * adb shell am broadcast --include-stopped-packages \
 *   -a dev.yamen.wirelessadb.CONFIGURE -n dev.yamen.wirelessadb/.ConfigReceiver \
 *   --es webhook_url http://<ha-host>:8123/api/webhook/<id> --es device_id <name>
 * # show the current setting: same command without extras
 * # turn the webhook off: --ez clear true
 * ```
 *
 * The result line of `am broadcast` shows the setting after the change.
 */
class ConfigReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_CONFIGURE) return

        val ok = if (intent.getBooleanExtra(EXTRA_CLEAR, false)) {
            WebhookConfig.clear(context)
        } else {
            WebhookConfig.save(
                context,
                url = intent.getStringExtra(EXTRA_WEBHOOK_URL),
                deviceId = intent.getStringExtra(EXTRA_DEVICE_ID),
            )
        }

        val config = WebhookConfig.load(context)
        val summary = "webhook_url=${config.url ?: "(off)"} device_id=${config.deviceId}"
        Log.i(TAG, if (ok) "config: $summary" else "config rejected; kept $summary")
        resultCode = if (ok) Activity.RESULT_OK else Activity.RESULT_CANCELED
        resultData = if (ok) summary else "error: webhook_url must start with http:// or https://; $summary"
    }

    companion object {
        private const val TAG = "WirelessAdbConfig"
        const val ACTION_CONFIGURE = "dev.yamen.wirelessadb.CONFIGURE"
        const val EXTRA_WEBHOOK_URL = "webhook_url"
        const val EXTRA_DEVICE_ID = "device_id"
        const val EXTRA_CLEAR = "clear"
    }
}
