package com.enuff.localwifidebug

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.WorkRequest
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * Headless equivalent of the old Activity auto-run: enable Wireless Debugging,
 * discover the assigned mDNS port, and POST it once to the HA webhook. Shared by
 * both triggers — BootReceiver (on boot) and MainActivity (manual/recovery).
 *
 * Retry boundary (agreed with the HA/add-on side):
 *  - The worker's ONLY success signal is the webhook's HTTP 2xx. It has zero
 *    visibility into whether the add-on subsequently connects over ADB, so it
 *    cannot and does not retry on downstream recovery. "Done" = the port was
 *    delivered to HA, never "ADB recovered".
 *  - Retries exist only to deliver ONE POST past transient conditions
 *    (network up via constraint, WD slow to advertise, webhook briefly
 *    unreachable), bounded to [MAX_ATTEMPTS]. No port discovered -> no POST is
 *    invented; the add-on's own mDNS watch is the backstop.
 */
class SyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val enqueueTs = inputData.getLong(KEY_ENQUEUE_TS, -1L)
        val latencyMs = if (enqueueTs > 0) System.currentTimeMillis() - enqueueTs else -1L
        // enqueue->run latency + attempt make silent expedited-quota degradation
        // observable: a large latency on attempt 0 means the job ran deferred.
        Log.d(TAG, "start: enqueue->run=${latencyMs}ms attempt=$runAttemptCount")

        createNotificationChannels()

        // Loud self-check: WRITE_SECURE_SETTINGS does NOT survive a
        // signature-changed reinstall and cannot be UI-granted, so a missing
        // grant would otherwise leave the app silently inert. Surface the exact
        // fix instead of failing quietly.
        if (!hasWriteSecureSettings()) {
            Log.e(TAG, "WRITE_SECURE_SETTINGS NOT granted — app is inert until granted")
            showNotification(
                "⚠️ WiFi Debug: permission missing",
                "Run: adb shell pm grant $PACKAGE android.permission.WRITE_SECURE_SETTINGS",
                ERROR_CHANNEL_ID,
            )
            return Result.failure()
        }

        try {
            enableWifiDebuggingIfNeeded()
        } catch (e: SecurityException) {
            // Defensive: permission could be revoked between the check and use.
            Log.e(TAG, "WRITE_SECURE_SETTINGS revoked mid-run", e)
            showNotification(
                "⚠️ WiFi Debug: permission missing",
                "Run: adb shell pm grant $PACKAGE android.permission.WRITE_SECURE_SETTINGS",
                ERROR_CHANNEL_ID,
            )
            return Result.failure()
        }

        val port = discoverPort(DISCOVERY_TIMEOUT_MS)
        if (port == null) {
            Log.w(TAG, "No $SERVICE_TYPE port discovered (attempt $runAttemptCount)")
            return if (runAttemptCount + 1 < MAX_ATTEMPTS) {
                Result.retry() // WD may still be spinning up
            } else {
                showNotification("WiFi Debug", "Port not found; HA mDNS watch is the backstop.")
                Result.failure()
            }
        }

        return if (postToHa(port)) {
            Log.d(TAG, "posted port=$port (latency ${latencyMs}ms)")
            showNotification("WiFi Debug Sync", "Port $port sent.")
            Result.success()
        } else {
            if (runAttemptCount + 1 < MAX_ATTEMPTS) {
                Result.retry()
            } else {
                showNotification("WiFi Debug Error", "Failed to POST port $port.", ERROR_CHANNEL_ID)
                Result.failure()
            }
        }
    }

    /** Required so expedited work does not crash on API < 31 (FGS fallback path). */
    override suspend fun getForegroundInfo(): ForegroundInfo {
        createNotificationChannels()
        val notification = NotificationCompat.Builder(applicationContext, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("WiFi Debug Sync")
            .setContentText("Reporting debug port…")
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()
        return ForegroundInfo(ONGOING_NOTIF_ID, notification)
    }

    private fun hasWriteSecureSettings(): Boolean =
        applicationContext.checkSelfPermission(
            android.Manifest.permission.WRITE_SECURE_SETTINGS,
        ) == PackageManager.PERMISSION_GRANTED

    private suspend fun enableWifiDebuggingIfNeeded() {
        val resolver = applicationContext.contentResolver
        val alreadyOn = Settings.Global.getInt(resolver, "adb_wifi_enabled", 0) == 1
        if (!alreadyOn) {
            Log.d(TAG, "Enabling Wireless Debugging")
            Settings.Global.putInt(resolver, "adb_wifi_enabled", 1)
            delay(ENABLE_SETTLE_MS) // let the service come up and start advertising
        } else {
            Log.d(TAG, "Wireless Debugging already enabled")
        }
    }

    /**
     * Find THIS device's own `_adb-tls-connect` port.
     *
     * mDNS discovery returns every device on the LAN with Wireless Debugging on
     * (phones, tablets, other TVs), not just this one. The first version took the
     * first service that resolved, and on 2026-11-09 and -10 it reported another Android device's
     * port to HA after the 03:01 standby restart. A resolved service only counts
     * if one of its addresses is one of this device's own interface addresses.
     *
     * Resolves run one at a time from a queue: older NsdManager stacks reject a
     * second concurrent resolve (FAILURE_ALREADY_ACTIVE), which could otherwise
     * drop our own service when several devices are found in quick succession.
     */
    private suspend fun discoverPort(timeoutMs: Long): Int? {
        val ownAddresses = localAddresses()
        if (ownAddresses.isEmpty()) {
            // Without our own addresses we can't tell our service from anyone
            // else's. Report nothing rather than a guess; the add-on's mDNS
            // watch is the backstop.
            Log.w(TAG, "Could not enumerate own interface addresses; skipping discovery")
            return null
        }
        return withTimeoutOrNull(timeoutMs) { discoverOwnPort(ownAddresses) }
    }

    private suspend fun discoverOwnPort(ownAddresses: Set<List<Byte>>): Int =
        suspendCancellableCoroutine { cont ->
            val nsd = applicationContext.getSystemService(Context.NSD_SERVICE) as NsdManager
            val lock = Any()
            val pending = ArrayDeque<NsdServiceInfo>()
            var resolving = false
            var done = false
            var listener: NsdManager.DiscoveryListener? = null

            fun stopDiscovery() {
                listener?.let {
                    try {
                        nsd.stopServiceDiscovery(it)
                    } catch (_: Exception) {
                    }
                }
                listener = null
            }

            fun resolveNext() {
                val next = synchronized(lock) {
                    if (done || resolving) return
                    pending.removeFirstOrNull()?.also { resolving = true }
                } ?: return

                nsd.resolveService(next, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                        Log.e(TAG, "Resolve failed for ${serviceInfo.serviceName}: $errorCode")
                        synchronized(lock) { resolving = false }
                        resolveNext()
                    }

                    override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                        val addresses = resolvedAddresses(serviceInfo)
                        val isOwn = addresses.any { it.address.toList() in ownAddresses }
                        val claimed = synchronized(lock) {
                            resolving = false
                            if (isOwn && !done) {
                                done = true
                                true
                            } else {
                                false
                            }
                        }
                        if (claimed) {
                            Log.d(TAG, "Own service ${serviceInfo.serviceName} on port ${serviceInfo.port}")
                            stopDiscovery()
                            if (cont.isActive) cont.resume(serviceInfo.port)
                        } else {
                            if (!isOwn) {
                                Log.d(
                                    TAG,
                                    "Skipping ${serviceInfo.serviceName} at " +
                                        "${addresses.map { it.hostAddress }}:${serviceInfo.port} - not this device",
                                )
                            }
                            resolveNext()
                        }
                    }
                })
            }

            listener = object : NsdManager.DiscoveryListener {
                override fun onDiscoveryStarted(regType: String) {}

                override fun onServiceFound(service: NsdServiceInfo) {
                    if (!service.serviceType.contains("adb-tls-connect")) return
                    synchronized(lock) { pending.addLast(service) }
                    resolveNext()
                }

                override fun onServiceLost(service: NsdServiceInfo) {}
                override fun onDiscoveryStopped(serviceType: String) {}
                override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                    Log.e(TAG, "Discovery start failed: $errorCode")
                    if (cont.isActive) cont.cancel()
                }
                override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            }

            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
            cont.invokeOnCancellation { stopDiscovery() }
        }

    /** Raw address bytes of every interface address on this device (scope-free, so IPv6 compares cleanly). */
    private fun localAddresses(): Set<List<Byte>> = try {
        NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
            .flatMap { it.inetAddresses.toList() }
            .filterNot { it.isLoopbackAddress }
            .map { it.address.toList() }
            .toSet()
    } catch (e: Exception) {
        Log.e(TAG, "Failed to enumerate network interfaces", e)
        emptySet()
    }

    private fun resolvedAddresses(info: NsdServiceInfo): List<InetAddress> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            info.hostAddresses.ifEmpty { listOfNotNull(@Suppress("DEPRECATION") info.host) }
        } else {
            listOfNotNull(@Suppress("DEPRECATION") info.host)
        }

    private suspend fun postToHa(port: Int): Boolean = withContext(Dispatchers.IO) {
        try {
            val body = JSONObject().apply {
                put("port", port)
                put("device_id", BuildConfig.DEVICE_ID)
            }.toString().toRequestBody("application/json".toMediaType())

            val request = Request.Builder()
                .url(BuildConfig.WEBHOOK_URL)
                .post(body)
                .build()

            OkHttpClient().newCall(request).execute().use { response ->
                Log.d(TAG, "HA response: ${response.code}")
                response.isSuccessful
            }
        } catch (e: Exception) {
            Log.e(TAG, "POST to HA failed", e)
            false
        }
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = notificationManager()
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "WiFi Debug Status",
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply { description = "Port reporting status" },
            )
            nm.createNotificationChannel(
                NotificationChannel(
                    ERROR_CHANNEL_ID,
                    "WiFi Debug Errors",
                    NotificationManager.IMPORTANCE_HIGH,
                ).apply { description = "Setup/permission problems that need attention" },
            )
        }
    }

    private fun showNotification(title: String, message: String, channelId: String = CHANNEL_ID) {
        val isError = channelId == ERROR_CHANNEL_ID
        val notification = NotificationCompat.Builder(applicationContext, channelId)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message)) // show the full command
            .setPriority(if (isError) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .build()
        notificationManager().notify(if (isError) ERROR_NOTIF_ID else RESULT_NOTIF_ID, notification)
    }

    private fun notificationManager() =
        applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        private const val TAG = "WiFiDebugWorker"
        private const val UNIQUE_NAME = "wifi_debug_sync"
        private const val PACKAGE = "com.enuff.localwifidebug"
        const val KEY_ENQUEUE_TS = "enqueue_ts"

        private const val SERVICE_TYPE = "_adb-tls-connect._tcp."
        private const val CHANNEL_ID = "wifi_debug_status"
        private const val ERROR_CHANNEL_ID = "wifi_debug_errors"
        private const val RESULT_NOTIF_ID = 1
        private const val ERROR_NOTIF_ID = 2
        private const val ONGOING_NOTIF_ID = 42

        private const val MAX_ATTEMPTS = 3
        private const val ENABLE_SETTLE_MS = 3000L
        private const val DISCOVERY_TIMEOUT_MS = 8000L

        /** Enqueue the sync, stamping the enqueue time for latency observability. */
        fun enqueue(context: Context) {
            val input = Data.Builder()
                .putLong(KEY_ENQUEUE_TS, System.currentTimeMillis())
                .build()

            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .setInputData(input)
                .setBackoffCriteria(
                    BackoffPolicy.LINEAR,
                    WorkRequest.MIN_BACKOFF_MILLIS,
                    TimeUnit.MILLISECONDS,
                )
                .build()

            WorkManager.getInstance(context)
                .enqueueUniqueWork(UNIQUE_NAME, ExistingWorkPolicy.REPLACE, request)
        }
    }
}
