package dev.yamen.wirelessadb

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Phase a: on device boot, enqueue the headless port sync.
 *
 * Android Wireless Debugging is disabled on reboot and comes back on a new
 * random port when re-enabled, so the device proactively pushes the fresh port
 * to Home Assistant instead of waiting to be launched. No UI is involved.
 *
 * Note: an app in Android's "stopped state" (freshly installed, or force-stopped)
 * does NOT receive BOOT_COMPLETED until it is launched once. That one-time launch
 * is part of the documented per-device migration.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action == Intent.ACTION_BOOT_COMPLETED ||
            action == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            Log.d(TAG, "Boot received ($action) -> enqueuing SyncWorker")
            SyncWorker.enqueue(context.applicationContext)
        }
    }

    companion object {
        private const val TAG = "WiFiDebugBoot"
    }
}
