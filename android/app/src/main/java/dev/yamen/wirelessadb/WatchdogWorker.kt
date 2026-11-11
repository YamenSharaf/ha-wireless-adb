package dev.yamen.wirelessadb

import android.content.Context
import android.content.pm.PackageManager
import android.provider.Settings
import android.util.Log
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * Keeps wireless debugging on while the device runs.
 *
 * The boot sync covers restarts, but Android can also turn wireless debugging
 * off without a reboot (network changes, adbd restarts, someone flipping the
 * toggle). Every 15 minutes (WorkManager's minimum period) this checks the
 * setting and turns it back on if needed. Cheap: one settings read, no network.
 *
 * If a webhook is configured, a needed re-enable goes through SyncWorker so the
 * new port is reported too.
 */
class WatchdogWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val ctx = applicationContext
        if (ctx.checkSelfPermission(android.Manifest.permission.WRITE_SECURE_SETTINGS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            // SyncWorker owns the "permission missing" notification.
            Log.w(TAG, "WRITE_SECURE_SETTINGS not granted; can't keep wireless debugging on")
            return Result.success()
        }

        val resolver = ctx.contentResolver
        if (Settings.Global.getInt(resolver, ADB_WIFI_ENABLED, 0) == 1) {
            return Result.success()
        }

        Log.i(TAG, "Wireless debugging was off; turning it back on")
        if (WebhookConfig.load(ctx).enabled) {
            SyncWorker.enqueue(ctx)
        } else {
            Settings.Global.putInt(resolver, ADB_WIFI_ENABLED, 1)
        }
        return Result.success()
    }

    companion object {
        private const val TAG = "WirelessAdbWatchdog"
        private const val UNIQUE_NAME = "wireless_debugging_watchdog"
        private const val ADB_WIFI_ENABLED = "adb_wifi_enabled"
        private const val PERIOD_MINUTES = 15L

        /** Idempotent: keeps one periodic watchdog, updated in place on app updates. */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<WatchdogWorker>(
                PERIOD_MINUTES,
                TimeUnit.MINUTES,
            ).build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(UNIQUE_NAME, ExistingPeriodicWorkPolicy.UPDATE, request)
        }
    }
}
