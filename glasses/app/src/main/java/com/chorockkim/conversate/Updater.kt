package com.chorockkim.conversate

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import java.io.File
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

/**
 * Automatic OTA. Same server API as the navigation app (/app/latest, /app/download), but installs
 * without any tap on the glasses:
 *
 * Android 12+ lets an app update *itself* with no confirmation screen through PackageInstaller
 * with USER_ACTION_NOT_REQUIRED, when
 *   - the app is the installer of record: install the first APK with
 *     `adb install -r -i com.chorockkim.conversate app.apk`, and later updates keep it that way;
 *   - "install unknown apps" is allowed for it:
 *     `adb shell appops set com.chorockkim.conversate REQUEST_INSTALL_PACKAGES allow`.
 * If Android still wants a confirmation (conditions not met), the system screen is opened as a
 * fallback. After a successful install the app is restarted by [UpdateReceiver] on
 * MY_PACKAGE_REPLACED. Only an APK signed with the same key installs (scripts/release.sh).
 *
 * Unverified on the glasses: whether Rokid's firmware honours USER_ACTION_NOT_REQUIRED.
 */
class Updater(
    private val context: Context,
    private val config: () -> ServerConfig,
    private val callbackExecutor: Executor,
    private val currentVersionCode: Int = BuildConfig.VERSION_CODE,
    private val http: OkHttpClient = OkHttpClient.Builder().readTimeout(60, TimeUnit.SECONDS).build(),
) {
    data class Latest(val versionCode: Int, val versionName: String, val size: Long, val sha256: String)

    interface Listener {
        /** Non-null when a newer version is on the server. */
        fun onUpdateAvailable(available: Latest?)
        /** Short status for the screen (download progress, errors). */
        fun onUpdateStatus(message: String)
    }

    var listener: Listener? = null

    @Volatile var available: Latest? = null
        private set
    @Volatile var busy = false
        private set

    /** Asks the server for the latest version (background thread). */
    fun check() {
        if (busy) return
        val cfg = config()
        if (!cfg.isComplete) return
        busy = true
        Thread({
            runCatching {
                val request = Request.Builder().url("${cfg.httpBase}/app/latest").auth(cfg).build()
                http.newCall(request).execute().use { r ->
                    available = when {
                        r.code == 404 -> null
                        !r.isSuccessful -> error("HTTP ${r.code}")
                        else -> parseLatest(r.body.string()).takeIf { it.versionCode > currentVersionCode }
                    }
                }
            }.onFailure { Log.w(TAG, "update check failed", it) }
            busy = false
            val found = available
            post { listener?.onUpdateAvailable(found) }
        }, "update-check").start()
    }

    /** Downloads [available], verifies the sha256 and installs it in place. */
    fun install() {
        val latest = available ?: return
        if (busy) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !context.packageManager.canRequestPackageInstalls()) {
            post {
                listener?.onUpdateStatus(
                    "업데이트 ${latest.versionName}: 설치 권한 없음 (adb shell appops set ${context.packageName} REQUEST_INSTALL_PACKAGES allow)"
                )
            }
            return
        }
        val cfg = config()
        busy = true
        post { listener?.onUpdateStatus("업데이트 ${latest.versionName} 받는 중… 0%") }
        Thread({
            val result = runCatching {
                val file = File(File(context.cacheDir, "updates").apply { mkdirs() }, "update.apk")
                val request = Request.Builder().url("${cfg.httpBase}/app/download").auth(cfg).build()
                val sha = http.newCall(request).execute().use { r ->
                    if (!r.isSuccessful) error("다운로드 실패 (${r.code})")
                    val total = r.body.contentLength().takeIf { it > 0 } ?: latest.size
                    r.body.byteStream().use { input ->
                        copyWithSha256(input, file, total) { pct ->
                            post { listener?.onUpdateStatus("업데이트 ${latest.versionName} 받는 중… $pct%") }
                        }
                    }
                }
                if (sha != latest.sha256) error("파일 확인 실패 (sha256 불일치)")
                commit(file)
            }
            busy = false
            post {
                result.fold(
                    onSuccess = { listener?.onUpdateStatus("업데이트 ${latest.versionName} 설치 중… 앱이 다시 시작됩니다") },
                    onFailure = { listener?.onUpdateStatus("업데이트 실패: ${it.message}") },
                )
            }
        }, "update-download").start()
    }

    /** Hands the APK to PackageInstaller, asking for no user action. */
    private fun commit(apk: File) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            }
        }
        val sessionId = installer.createSession(params)
        installer.openSession(sessionId).use { session ->
            apk.inputStream().use { input ->
                session.openWrite("update.apk", 0, apk.length()).use { out ->
                    input.copyTo(out)
                    session.fsync(out)
                }
            }
            val intent = Intent(context, UpdateReceiver::class.java).setAction(UpdateReceiver.ACTION_INSTALL_STATUS)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            val pending = PendingIntent.getBroadcast(context, sessionId, intent, flags)
            session.commit(pending.intentSender)
        }
    }

    private fun post(block: () -> Unit) = callbackExecutor.execute(block)

    private fun Request.Builder.auth(cfg: ServerConfig) = apply {
        if (cfg.token.isNotEmpty()) header("Authorization", "Bearer ${cfg.token}")
    }

    companion object {
        private const val TAG = "Updater"

        fun parseLatest(json: String): Latest = JSONObject(json).let {
            Latest(it.getInt("versionCode"), it.getString("versionName"), it.getLong("size"), it.getString("sha256"))
        }

        /** Copies [input] to [file], reporting progress every 10 %, and returns the sha256 (hex). */
        fun copyWithSha256(input: InputStream, file: File, total: Long, onProgress: (Int) -> Unit = {}): String {
            val md = MessageDigest.getInstance("SHA-256")
            var got = 0L
            var lastPct = -1
            file.outputStream().use { out ->
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    md.update(buf, 0, n)
                    got += n
                    val pct = if (total > 0) (got * 100 / total).toInt().coerceAtMost(100) else 0
                    if (pct != lastPct && pct % 10 == 0) { lastPct = pct; onProgress(pct) }
                }
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }
    }
}

/**
 * - Install status from PackageInstaller: if Android insists on a confirmation, open its screen.
 * - MY_PACKAGE_REPLACED: the update went in and killed the old process; start the app again.
 */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            ACTION_INSTALL_STATUS -> {
                val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
                Log.i(TAG, "install status $status ${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)}")
                if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                    @Suppress("DEPRECATION")
                    val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                    confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let(context::startActivity)
                }
            }
            Intent.ACTION_MY_PACKAGE_REPLACED -> {
                Log.i(TAG, "updated to ${BuildConfig.VERSION_NAME}, restarting")
                context.startActivity(
                    Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            }
        }
    }

    companion object {
        private const val TAG = "UpdateReceiver"
        const val ACTION_INSTALL_STATUS = "com.chorockkim.conversate.INSTALL_STATUS"
    }
}
