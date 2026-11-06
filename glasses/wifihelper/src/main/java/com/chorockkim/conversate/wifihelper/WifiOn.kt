package com.chorockkim.conversate.wifihelper

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Bundle
import android.provider.Settings
import android.util.Log

/**
 * Turns Wi-Fi on (allowed because this app targets API 28, see build.gradle.kts) and tells the
 * main app how it went, so its Wi-Fi card can show why it did not work (no adb on the glasses).
 */
object WifiOn {
    private const val TAG = "WifiHelper"
    private const val MAIN = "com.chorockkim.conversate"
    const val RESULT = "com.chorockkim.conversate.wifihelper.RESULT"

    fun run(context: Context, via: String) {
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val airplane = runCatching {
            Settings.Global.getInt(context.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) == 1
        }.getOrDefault(false)
        var error = ""
        val ok = runCatching {
            @Suppress("DEPRECATION")
            wifi.isWifiEnabled || wifi.setWifiEnabled(true)
        }.getOrElse { error = "${it.javaClass.simpleName}: ${it.message}"; false }
        Log.i(TAG, "via $via: setWifiEnabled -> $ok, airplane=$airplane, state=${wifi.wifiState} $error")
        context.sendBroadcast(
            Intent(RESULT).setPackage(MAIN)
                .putExtra("ok", ok).putExtra("via", via).putExtra("airplane", airplane)
                .putExtra("state", wifi.wifiState).putExtra("error", error)
        )
    }
}

/** Background path: an explicit broadcast from the main app. */
class WifiOnReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = WifiOn.run(context, "receiver")
}

/** Foreground path (in case the system refuses a background app): no UI, finishes at once. */
class WifiOnActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WifiOn.run(this, "activity")
        finish()
    }
}
