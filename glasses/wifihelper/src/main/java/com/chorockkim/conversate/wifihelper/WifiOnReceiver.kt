package com.chorockkim.conversate.wifihelper

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.util.Log

/** Turns Wi-Fi on. Allowed because this app targets API 28 (see build.gradle.kts). */
class WifiOnReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val ok = runCatching {
            @Suppress("DEPRECATION")
            wifi.isWifiEnabled || wifi.setWifiEnabled(true)
        }.getOrElse { Log.w(TAG, "setWifiEnabled failed", it); false }
        Log.i(TAG, "Wi-Fi on requested: $ok")
    }

    private companion object {
        const val TAG = "WifiHelper"
    }
}
