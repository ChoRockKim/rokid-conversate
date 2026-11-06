package com.chorockkim.conversate

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.provider.Settings
import android.util.Log

/**
 * Keeps Wi-Fi on and reports where it stands, for the app's own Wi-Fi card ([Status]).
 *
 * The glasses keep Wi-Fi off to save battery, and this app needs it for the server. Turning it
 * on is the same sequence as Rokid-Maps' WifiConnector:
 *   1. Settings.Global.WIFI_ON = 1 (needs WRITE_SECURE_SETTINGS, granted over adb:
 *      `adb shell pm grant com.chorockkim.conversate android.permission.WRITE_SECURE_SETTINGS`)
 *   2. WifiManager.setWifiEnabled(true)
 *   3. IWifiManager.setWifiEnabled through reflection
 *   4. the "Wi-Fi 켜기 도우미" app, if installed: apps targeting API 29+ (this one) cannot turn
 *      Wi-Fi on without the adb grant, but one targeting 28 still can (wifihelper module).
 * It no longer opens the system Wi-Fi settings by itself: that screen is bright and large on
 * the glasses and hid the view. While Wi-Fi is off it retries every [RETRY_MS]; the system
 * screen opens only when the wearer taps the card ([openSystemSettings]).
 * Joining a network is left to the system (saved networks, e.g. the iPhone hotspot): apps
 * cannot pick a network on Android 12 without a system dialog.
 * Unverified on the glasses which of the enable calls the Rokid firmware honours.
 */
class WifiEnabler(private val context: Context, private val onStatus: (Status) -> Unit) {
    enum class Status { ENABLING, CANNOT_ENABLE, WAITING_FOR_NETWORK, CONNECTED }

    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private val connectivity =
        context.applicationContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    @Volatile private var running = false

    val isOn: Boolean get() = runCatching { wifi.isWifiEnabled }.getOrDefault(true)

    val isConnected: Boolean
        get() = runCatching {
            connectivity.getNetworkCapabilities(connectivity.activeNetwork)
                ?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }.getOrDefault(true)

    /** Watches until stopped; [onStatus] is called (from a background thread) on each change. */
    fun start() {
        if (running) return
        running = true
        Thread({
            var last: Status? = null
            var offSince = 0L
            var lastTry = 0L
            while (running) {
                val now = System.currentTimeMillis()
                val status = when {
                    isConnected -> Status.CONNECTED.also { offSince = 0L }
                    isOn -> Status.WAITING_FOR_NETWORK.also { offSince = 0L }
                    else -> {
                        if (offSince == 0L) offSince = now
                        if (now - lastTry >= RETRY_MS) { lastTry = now; tryEnable() }
                        if (now - offSince >= GIVE_UP_MS) Status.CANNOT_ENABLE else Status.ENABLING
                    }
                }
                if (status != last) {
                    Log.i(TAG, "Wi-Fi: $status")
                    last = status
                    onStatus(status)
                }
                Thread.sleep(if (status == Status.CONNECTED) CONNECTED_POLL_MS else POLL_MS)
            }
        }, "wifi-watch").apply { isDaemon = true }.start()
    }

    fun stop() { running = false }

    /** Only on the wearer's request (tap on the Wi-Fi card). */
    fun openSystemSettings() {
        runCatching {
            context.startActivity(Intent(Settings.ACTION_WIFI_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    /** The "Wi-Fi 켜기 도우미" app (wifihelper module) is installed. */
    val hasHelper: Boolean
        get() = runCatching { context.packageManager.getPackageInfo(HELPER, 0); true }.getOrDefault(false)

    @Suppress("DEPRECATION")
    private fun tryEnable() {
        // The helper targets API 28, where apps may still switch Wi-Fi on; this app cannot.
        if (hasHelper) runCatching {
            context.sendBroadcast(
                Intent(HELPER_ACTION).setClassName(HELPER, "$HELPER.WifiOnReceiver")
                    .addFlags(Intent.FLAG_INCLUDE_STOPPED_PACKAGES)
            )
            Log.i(TAG, "asked the Wi-Fi helper")
        }.onFailure { Log.i(TAG, "Wi-Fi helper failed: ${it.message}") }
        runCatching {
            val ok = Settings.Global.putInt(context.contentResolver, Settings.Global.WIFI_ON, 1)
            Log.i(TAG, "Settings.Global.WIFI_ON=1 result=$ok")
        }.onFailure { Log.i(TAG, "Settings.Global.WIFI_ON failed: ${it.message}") }
        runCatching {
            val ok = wifi.setWifiEnabled(true)
            Log.i(TAG, "setWifiEnabled(true) result=$ok")
        }.onFailure { Log.i(TAG, "setWifiEnabled failed: ${it.message}") }
        runCatching {
            val iface = Class.forName("android.net.wifi.IWifiManager")
            val field = WifiManager::class.java.getDeclaredField("mService").apply { isAccessible = true }
            val service = field.get(wifi)
            val ok = iface.getMethod("setWifiEnabled", String::class.java, Boolean::class.javaPrimitiveType)
                .invoke(service, context.packageName, true)
            Log.i(TAG, "IWifiManager.setWifiEnabled(true) result=$ok")
        }.onFailure { Log.i(TAG, "IWifiManager reflection failed: ${it.message}") }
    }

    private companion object {
        const val TAG = "WifiEnabler"
        const val HELPER = "com.chorockkim.conversate.wifihelper"
        const val HELPER_ACTION = "$HELPER.WIFI_ON"
        const val POLL_MS = 1_000L
        const val CONNECTED_POLL_MS = 5_000L
        const val RETRY_MS = 10_000L
        /** Still off after this long: say so (and offer the system screen on tap). */
        const val GIVE_UP_MS = 8_000L
    }
}
