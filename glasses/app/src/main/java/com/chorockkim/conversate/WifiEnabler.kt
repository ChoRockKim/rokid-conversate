package com.chorockkim.conversate

import android.content.BroadcastReceiver
import android.content.Context
import android.content.IntentFilter
import android.os.Build
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
class WifiEnabler(private val context: Context, private val onStatus: (Status, String?) -> Unit) {
    enum class Status { ENABLING, CANNOT_ENABLE, WAITING_FOR_NETWORK, CONNECTED }

    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private val connectivity =
        context.applicationContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    @Volatile private var running = false
    /** What the helper last reported, shown on the Wi-Fi card (there is no adb on the glasses). */
    @Volatile var helperReport: String? = null
        private set
    private var attempts = 0
    private val resultReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            val ok = intent.getBooleanExtra("ok", false)
            val airplane = intent.getBooleanExtra("airplane", false)
            val state = intent.getIntExtra("state", -1)
            val error = intent.getStringExtra("error").orEmpty()
            val via = intent.getStringExtra("via").orEmpty()
            helperReport = when {
                airplane -> "비행기 모드가 켜져 있습니다"
                !ok && error.isNotEmpty() -> "도우미 오류: $error"
                !ok -> "시스템이 도우미의 Wi-Fi 켜기를 거부했습니다"
                else -> "켜기 요청은 받아들여졌지만 아직 꺼져 있음 (상태 $state)"
            } + " [$via]"
            Log.i(TAG, "helper: $helperReport")
        }
    }

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
        runCatching {
            val filter = IntentFilter(HELPER_RESULT)
            if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(resultReceiver, filter, Context.RECEIVER_EXPORTED)
            else context.registerReceiver(resultReceiver, filter)
        }
        Thread({
            var last: Pair<Status, String?>? = null
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
                val detail = if (status == Status.CANNOT_ENABLE) detail() else null
                if (status to detail != last) {
                    Log.i(TAG, "Wi-Fi: $status $detail")
                    last = status to detail
                    onStatus(status, detail)
                }
                Thread.sleep(if (status == Status.CONNECTED) CONNECTED_POLL_MS else POLL_MS)
            }
        }, "wifi-watch").apply { isDaemon = true }.start()
    }

    fun stop() {
        running = false
        runCatching { context.unregisterReceiver(resultReceiver) }
    }

    private fun detail(): String = when {
        !hasHelper -> "'Wi-Fi 켜기 도우미' 앱이 없습니다"
        else -> helperReport ?: "도우미 응답 없음"
    }

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
        // Its no-UI activity first: on the glasses the broadcast got no answer the first time
        // (the helper had never run, and the system does not wake such an app for it), then
        // the activity turned Wi-Fi on (2026-11-06). The broadcast stays as the other attempt.
        if (hasHelper) runCatching {
            if (attempts++ % 2 == 0) {
                context.startActivity(
                    Intent().setClassName(HELPER, "$HELPER.WifiOnActivity").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            } else {
                context.sendBroadcast(
                    Intent(HELPER_ACTION).setClassName(HELPER, "$HELPER.WifiOnReceiver")
                        .addFlags(Intent.FLAG_INCLUDE_STOPPED_PACKAGES)
                )
            }
            Log.i(TAG, "asked the Wi-Fi helper (attempt $attempts)")
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
        const val HELPER_RESULT = "$HELPER.RESULT"
        const val POLL_MS = 1_000L
        const val CONNECTED_POLL_MS = 5_000L
        const val RETRY_MS = 10_000L
        /** Still off after this long: say so (and offer the system screen on tap). */
        const val GIVE_UP_MS = 8_000L
    }
}
