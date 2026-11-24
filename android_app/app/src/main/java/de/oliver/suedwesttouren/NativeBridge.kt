package de.oliver.suedwesttouren

import android.content.ActivityNotFoundException
import android.content.Intent
import android.util.Log
import android.webkit.JavascriptInterface
import androidx.core.content.FileProvider
import java.io.File

/** window.SWNative – the small native API the shared web UI uses inside the app. */
class NativeBridge(private val activity: MainActivity) {

    /**
     * Writes the GPX into the app cache and opens it directly in Mapy (cz.seznam.mapy) via FileProvider
     * (ACTION_VIEW, application/gpx+xml, read grant) – no detour through the Download folder.
     * Returns a short status text for a toast.
     */
    @JavascriptInterface
    fun openGpx(fileName: String, xml: String, title: String): String {
        val dir = File(activity.cacheDir, "gpx").apply { mkdirs() }
        dir.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 24 * 3600_000L }?.forEach { it.delete() }
        val safe = fileName.replace(Regex("[^A-Za-z0-9._-]"), "_").ifBlank { "tour.gpx" }
        val file = File(dir, if (safe.endsWith(".gpx")) safe else "$safe.gpx")
        file.writeText(xml, Charsets.UTF_8)
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.files", file)
        val view = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/gpx+xml")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        val hasMapy = activity.isInstalled(MainActivity.MAPY)
        if (hasMapy) view.setPackage(MainActivity.MAPY)
        activity.runOnUiThread {
            try {
                activity.startActivity(if (hasMapy) view else Intent.createChooser(view, title).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                Log.i(MainActivity.TAG, "GPX → ${if (hasMapy) MainActivity.MAPY else "chooser"} $uri (${file.length()} B)")
                if (hasMapy) activity.watchLaunch(MainActivity.MAPY)
            } catch (e: ActivityNotFoundException) {
                Log.w(MainActivity.TAG, "no GPX app", e)
            }
        }
        return if (hasMapy) "GPX → Mapy" else "GPX: App wählen"
    }

    @JavascriptInterface
    fun isInstalled(pkg: String): Boolean = activity.isInstalled(pkg)

    @JavascriptInterface
    fun openAppSettings(pkg: String) {
        activity.runOnUiThread { activity.openAppSettings(pkg) }
    }

    @JavascriptInterface
    fun setDarkTheme(dark: Boolean) {
        activity.runOnUiThread { activity.applySystemBars(dark) }
    }

    @JavascriptInterface
    fun contentReady(jsMillis: Double) {
        activity.runOnUiThread { activity.onContentReady(jsMillis) }
    }

    @JavascriptInterface
    fun appVersion(): String = BuildConfigInfo.VERSION

    @JavascriptInterface
    fun exportJson(fileName: String, json: String, title: String): Boolean {
        activity.runOnUiThread { activity.exportJson(fileName, json, title) }
        return true
    }

    @JavascriptInterface
    fun hasNotificationPermission(): Boolean = activity.hasNotificationPermission()

    @JavascriptInterface
    fun requestNotificationPermission() {
        activity.runOnUiThread { activity.requestNotificationPermission() }
    }

    @JavascriptInterface
    fun scheduleReminder(id: String, triggerAtMillis: Long, title: String, text: String): String =
        activity.scheduleReminder(id, triggerAtMillis, title, text)

    @JavascriptInterface
    fun cancelReminder(id: String): Boolean =
        activity.cancelReminder(id)
}

internal object BuildConfigInfo {
    const val VERSION = "1.0"
}
