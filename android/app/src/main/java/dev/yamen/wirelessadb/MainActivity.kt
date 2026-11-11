package dev.yamen.wirelessadb

import android.app.Activity
import android.os.Bundle
import android.util.Log

/**
 * Phase b: MainActivity is now a headless launcher.
 *
 * Both entry points — HA's `remote.turn_on ... activity` / ADB `monkey LAUNCHER`
 * (manual + recovery) and the app icon — route through the SAME expedited
 * [SyncWorker] the boot path uses. The Activity does no UI work: with
 * Theme.Invisible (windowNoDisplay) it never shows a window, enqueues the worker,
 * and finishes in onCreate before it could become visible — so the launch no
 * longer takes over the TV screen.
 *
 * The LAUNCHER + LEANBACK_LAUNCHER intent filters are kept so HA's existing
 * launch and ADB `monkey` continue to work unchanged, and so launching also
 * pulls the app out of Android's "stopped state" (re-arming BOOT_COMPLETED).
 *
 * A third entry point, the app link `wirelessadb://launch`, reaches the same
 * onCreate. HA should launch with that link: a bare package name becomes
 * `market://launch?id=...`, which stopped resolving on the Bravia after the
 * Play Store update of ~2026-10-20. The old `localwifidebug://launch` link
 * (from before the app was renamed) is still accepted for existing automations.
 */
class MainActivity : Activity() {

    @Suppress("DEPRECATION") // overridePendingTransition: fine, we want zero animation
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.d(TAG, "Headless launch -> enqueuing SyncWorker")
        SyncWorker.enqueue(applicationContext)
        finish()
        // Belt-and-suspenders: kill any close transition (theme already disables
        // window animation) so nothing paints a stray frame on this
        // refresh-rate-switching panel.
        overridePendingTransition(0, 0)
    }

    companion object {
        private const val TAG = "WiFiDebugActivity"
    }
}
