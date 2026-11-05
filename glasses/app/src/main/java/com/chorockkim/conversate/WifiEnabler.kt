package com.chorockkim.conversate

import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.provider.Settings
import android.util.Log

/**
 * Turns Wi-Fi on when the app starts and it is off (the glasses keep Wi-Fi off to save battery,
 * and this app needs it for the server). Same sequence as Rokid-Maps' WifiConnector, which does
 * this on Rokid glasses:
 *   1. Settings.Global.WIFI_ON = 1 (needs WRITE_SECURE_SETTINGS, granted over adb:
 *      `adb shell pm grant com.chorockkim.conversate android.permission.WRITE_SECURE_SETTINGS`)
 *   2. WifiManager.setWifiEnabled(true)
 *   3. IWifiManager.setWifiEnabled through reflection
 * If Wi-Fi is still off after a few seconds, the Wi-Fi settings screen is opened.
 * Unverified on the glasses which of these the Rokid firmware honours.
 */
class WifiEnabler(private val context: Context, private val onStatus: (String) -> Unit) {
    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager

    val isOn: Boolean get() = runCatching { wifi.isWifiEnabled }.getOrDefault(true)

    /** Runs on a background thread; [onStatus] is called from it. */
    fun ensureOn() {
        if (isOn) return
        Thread({
            onStatus("Wi-Fi 켜는 중…")
            tryEnable()
            repeat(POLLS) {
                if (isOn) {
                    Log.i(TAG, "Wi-Fi on after ${(it + 1) * POLL_MS} ms")
                    onStatus("Wi-Fi 켜짐")
                    return@Thread
                }
                Thread.sleep(POLL_MS)
            }
            Log.w(TAG, "Wi-Fi still off, opening settings")
            onStatus("Wi-Fi를 켜 주세요")
            runCatching {
                context.startActivity(Intent(Settings.ACTION_WIFI_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        }, "wifi-enable").start()
    }

    @Suppress("DEPRECATION")
    private fun tryEnable() {
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
        const val POLLS = 16
        const val POLL_MS = 500L
    }
}
