package de.oliver.suedwesttouren

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlarmManager
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Process
import android.os.SystemClock
import android.util.Log
import android.view.View
import android.view.ViewTreeObserver
import android.webkit.ConsoleMessage
import android.webkit.GeolocationPermissions
import android.webkit.MimeTypeMap
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewClientCompat
import org.json.JSONObject
import java.io.File

/**
 * Südwest-Touren as a native app: the shared UI (assets/www/index.html, data + WebP photos inside the APK)
 * runs in a WebView served from https://appassets.androidplatform.net (secure origin, no network needed).
 * Everything that leaves the app is a real Android intent (DB Navigator, München Navigator, NVV, myVRN, Mapy).
 */
class MainActivity : ComponentActivity() {

    companion object {
        const val TAG = "SuedwestTouren"
        const val START_URL = "https://${WebViewAssetLoader.DEFAULT_DOMAIN}/assets/www/index.html"
        const val MAPY = "cz.seznam.mapy"
    }

    lateinit var webView: WebView
        private set
    private lateinit var root: FrameLayout
    private lateinit var tiles: TileCache
    @Volatile var contentReady = false
    private var pendingGeo: Pair<String, GeolocationPermissions.Callback>? = null
    private var pendingFilePathCallback: ValueCallback<Array<Uri>>? = null
    private var pendingExportJson: String? = null
    private data class PendingReminder(val id: String, val triggerAtMillis: Long, val title: String, val text: String)
    private var pendingReminder: PendingReminder? = null
    private var pendingOpenTab: String? = null

    private val locationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        pendingGeo?.let { (origin, cb) -> cb.invoke(origin, granted, false) }
        pendingGeo = null
    }

    private val filePicker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        val cb = pendingFilePathCallback
        pendingFilePathCallback = null
        cb?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
    }

    private val jsonSaver = registerForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri: Uri? ->
        val json = pendingExportJson
        pendingExportJson = null
        if (uri != null && json != null) {
            try {
                contentResolver.openOutputStream(uri)?.use { it.write(json.toByteArray(Charsets.UTF_8)) }
                webView.evaluateJavascript("window.toast && toast('Export gespeichert')", null)
            } catch (e: Exception) {
                Log.w(TAG, "exportJson write failed", e)
                webView.evaluateJavascript("window.toast && toast('Export fehlgeschlagen')", null)
            }
        }
    }

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val pending = pendingReminder
        pendingReminder = null
        if (granted) {
            if (pending != null) {
                val res = scheduleAlarmInternal(pending.id, pending.triggerAtMillis, pending.title, pending.text)
                val cbId = JSONObject.quote(pending.id)
                val cbRes = JSONObject.quote(res)
                webView.evaluateJavascript("window.onSwReminderResult && window.onSwReminderResult($cbId, $cbRes)", null)
            }
        } else {
            val msg = JSONObject.quote("Mitteilungen für Erinnerungen deaktiviert")
            webView.evaluateJavascript(
                "window.toast && toast($msg, {label: 'Einstellungen', ms: 8000, action: () => SWNative.openAppSettings(${JSONObject.quote(packageName)})})",
                null
            )
            if (pending != null) {
                val cbId = JSONObject.quote(pending.id)
                webView.evaluateJavascript("window.onSwReminderResult && window.onSwReminderResult($cbId, 'denied')", null)
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        tiles = TileCache(this)
        ReminderReceiver.ensureChannel(this)
        pendingOpenTab = intent?.getStringExtra(ReminderReceiver.EXTRA_OPEN_TAB)?.takeIf { it.isNotBlank() }

        val assetLoader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        root = FrameLayout(this)
        webView = WebView(this)
        root.addView(webView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(root)
        applySystemBars(isNight())

        // Edge-to-edge (enforced on Android 15): keep the web UI clear of status/navigation bars and the keyboard
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val b = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime()
            )
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }

        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            setGeolocationEnabled(true)
            setSupportMultipleWindows(false)
            javaScriptCanOpenWindowsAutomatically = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }
        webView.setBackgroundColor(ContextCompat.getColor(this, R.color.window_bg))
        webView.addJavascriptInterface(NativeBridge(this), "SWNative")

        webView.webViewClient = object : WebViewClientCompat() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url
                if (TileCache.isTileHost(url.host)) return tiles.load(request)
                return assetLoader.shouldInterceptRequest(url)
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (url.host == WebViewAssetLoader.DEFAULT_DOMAIN) return false
                return openExternal(url.toString())
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
                    callback.invoke(origin, true, false)
                } else {
                    pendingGeo = origin to callback
                    locationPermission.launch(Manifest.permission.ACCESS_FINE_LOCATION)
                }
            }

            override fun onConsoleMessage(msg: ConsoleMessage): Boolean {
                if (msg.messageLevel() == ConsoleMessage.MessageLevel.ERROR) {
                    Log.w(TAG, "JS ${msg.sourceId().substringAfterLast('/')}:${msg.lineNumber()} ${msg.message()}")
                }
                return true
            }

            override fun onShowFileChooser(
                webView: WebView,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams
            ): Boolean {
                pendingFilePathCallback?.onReceiveValue(null)
                pendingFilePathCallback = filePathCallback
                val mimeTypes = fileChooserParams.acceptTypes
                    .mapNotNull { raw ->
                        val s = raw.trim()
                        when {
                            s.isEmpty() -> null
                            s.equals(".json", ignoreCase = true) -> "application/json"
                            s.startsWith(".") -> MimeTypeMap.getSingleton()
                                .getMimeTypeFromExtension(s.removePrefix(".").lowercase())
                            else -> s
                        }
                    }
                    .distinct()
                    .toMutableList()
                if ("*/*" !in mimeTypes) mimeTypes.add("*/*")
                return try {
                    filePicker.launch(mimeTypes.toTypedArray())
                    true
                } catch (e: ActivityNotFoundException) {
                    Log.w(TAG, "no file chooser", e)
                    pendingFilePathCallback?.onReceiveValue(null)
                    pendingFilePathCallback = null
                    true
                }
            }
        }

        // Keep the system splash until the page has rendered (max. 1.5 s) → no blank WebView frame
        val t0 = SystemClock.uptimeMillis()
        root.viewTreeObserver.addOnPreDrawListener(object : ViewTreeObserver.OnPreDrawListener {
            override fun onPreDraw(): Boolean {
                if (contentReady || SystemClock.uptimeMillis() - t0 > 1500) {
                    root.viewTreeObserver.removeOnPreDrawListener(this)
                    return true
                }
                return false
            }
        })

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                webView.evaluateJavascript("window.swHandleBack ? String(window.swHandleBack()) : 'false'") { result ->
                    if (result?.contains("true") != true) {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                        isEnabled = true
                    }
                }
            }
        })

        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            webView.loadUrl(START_URL)
        }
    }

    /** Called from JS once the list is rendered. */
    fun onContentReady(jsMillis: Double) {
        if (contentReady) return
        contentReady = true
        val sinceProcess = SystemClock.elapsedRealtime() - Process.getStartElapsedRealtime()
        Log.i(TAG, "content ready: ${sinceProcess} ms since process start (JS ${jsMillis.toInt()} ms after navigation start)")
        root.post { root.invalidate() }
        val openTab = pendingOpenTab
        pendingOpenTab = null
        if (!openTab.isNullOrBlank()) {
            webView.post {
                val quoted = JSONObject.quote(openTab)
                webView.evaluateJavascript("window.swOpenTab && window.swOpenTab($quoted)", null)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val openTab = intent.getStringExtra(ReminderReceiver.EXTRA_OPEN_TAB)?.takeIf { it.isNotBlank() } ?: return
        if (contentReady) {
            val quoted = JSONObject.quote(openTab)
            webView.post {
                webView.evaluateJavascript("window.swOpenTab && window.swOpenTab($quoted)", null)
            }
        } else {
            pendingOpenTab = openTab
        }
    }

    fun isNight(): Boolean =
        (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

    /** Match status/navigation bar colours & icon contrast to the in-app theme (which can differ from the system). */
    fun applySystemBars(dark: Boolean) {
        val bg = if (dark) Color.parseColor("#0D1513") else Color.parseColor("#F3F5F1")
        root.setBackgroundColor(bg)
        if (::webView.isInitialized) webView.setBackgroundColor(bg)
        @Suppress("DEPRECATION")
        run {
            window.statusBarColor = bg
            window.navigationBarColor = bg
        }
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = !dark
            isAppearanceLightNavigationBars = !dark
        }
    }

    fun isInstalled(pkg: String): Boolean = try {
        packageManager.getPackageInfo(pkg, 0)
        true
    } catch (e: PackageManager.NameNotFoundException) {
        false
    }

    /**
     * Hands a link to the right app with a real Intent:
     * intent://…#Intent;scheme=dbnav|muenchennavigator|nvv;package=…  (DB Navigator, München Navigator, NVV)
     * intent:lat,lon?q=…#Intent;scheme=geo;package=com.mdv.VRNCompanion (myVRN)
     * https://mapy.com/fnc/v1/…  → Mapy app;  geo:… → app chooser;  other http(s) → browser / app links
     */
    fun openExternal(url: String): Boolean {
        try {
            if (url.startsWith("intent:")) {
                val intent = Intent.parseUri(url, Intent.URI_INTENT_SCHEME).apply {
                    addCategory(Intent.CATEGORY_BROWSABLE)
                    component = null
                    selector = null
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                return try {
                    startActivity(intent)
                    Log.i(TAG, "intent → ${intent.`package`} ${intent.data}")
                    watchLaunch(intent.`package`)
                    true
                } catch (e: ActivityNotFoundException) {
                    val fallback = intent.getStringExtra("browser_fallback_url")
                    Log.w(TAG, "app ${intent.`package`} missing, fallback $fallback")
                    if (!fallback.isNullOrBlank()) openExternal(fallback) else true
                }
            }
            val uri = Uri.parse(url)
            val intent = Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            val host = uri.host.orEmpty()
            if ((host == "mapy.com" || host.endsWith("mapy.cz")) && isInstalled(MAPY)) intent.setPackage(MAPY)
            startActivity(intent)
            Log.i(TAG, "view → ${intent.`package` ?: "*"} $uri")
            watchLaunch(intent.`package`)
        } catch (e: Exception) {
            Log.w(TAG, "cannot open $url", e)
            runOnUiThread { webView.evaluateJavascript("window.toast && toast('Keine App für diesen Link gefunden')", null) }
        }
        return true
    }

    /**
     * Some OEM ROMs (ZTE/Nubia MyOS: „Secondary launch“, MIUI: „Autostart“) silently drop starts of apps whose
     * process is not running. If we are still in the foreground shortly after starting another app, tell the
     * user and offer the target app's settings page.
     */
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    @Volatile private var pendingLaunch: String? = null

    fun watchLaunch(pkg: String?) {
        if (pkg.isNullOrBlank()) return
        pendingLaunch = pkg
        handler.postDelayed({
            if (pendingLaunch == pkg && lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
                pendingLaunch = null
                val label = try {
                    packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
                } catch (e: Exception) { pkg }
                Log.w(TAG, "launch of $pkg was not brought to front (OEM launch restriction?)")
                val msg = org.json.JSONObject.quote("$label wurde vom System nicht geöffnet – Start durch andere Apps erlauben?")
                webView.evaluateJavascript(
                    "window.toast && toast($msg, {label: 'Einstellungen', ms: 10000, action: () => SWNative.openAppSettings(${org.json.JSONObject.quote(pkg)})})",
                    null
                )
            }
        }, 2500)
    }

    fun openAppSettings(pkg: String) {
        startActivity(
            Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun exportJson(fileName: String, json: String, title: String) {
        val safe = fileName.replace(Regex("[^A-Za-z0-9._-]"), "_").ifBlank { "suedwest-touren-gemerkt.json" }
        val finalName = if (safe.endsWith(".json")) safe else "$safe.json"
        pendingExportJson = json
        try {
            jsonSaver.launch(finalName)
        } catch (e: ActivityNotFoundException) {
            pendingExportJson = null
            try {
                val dir = File(cacheDir, "export").apply { mkdirs() }
                val file = File(dir, finalName).apply { writeText(json, Charsets.UTF_8) }
                val uri = FileProvider.getUriForFile(this, "$packageName.files", file)
                val share = Intent(Intent.ACTION_SEND)
                    .setType("application/json")
                    .putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                startActivity(Intent.createChooser(share, title))
            } catch (ex: Exception) {
                Log.w(TAG, "exportJson fallback failed", ex)
            }
        }
    }

    fun hasNotificationPermission(): Boolean {
        val nm = getSystemService(NotificationManager::class.java)
        val enabled = nm?.areNotificationsEnabled() ?: true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return enabled && ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
        }
        return enabled
    }

    fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else if (!hasNotificationPermission()) {
            openAppSettings(packageName)
        }
    }

    fun scheduleReminder(id: String, triggerAtMillis: Long, title: String, text: String): String {
        if (id.isBlank()) return "error:empty_id"
        if (triggerAtMillis <= System.currentTimeMillis()) return "error:past_time"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            runOnUiThread {
                pendingReminder = PendingReminder(id, triggerAtMillis, title, text)
                notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
            return "permission_needed"
        }
        if (!hasNotificationPermission()) return "notifications_disabled"
        return scheduleAlarmInternal(id, triggerAtMillis, title, text)
    }

    private fun reminderPendingIntent(id: String, title: String, text: String, flags: Int): PendingIntent? {
        val intent = Intent(this, ReminderReceiver::class.java).apply {
            action = ReminderReceiver.ACTION_REMINDER
            data = Uri.parse("swreminder://$id")
            putExtra(ReminderReceiver.EXTRA_ID, id)
            putExtra(ReminderReceiver.EXTRA_TITLE, title)
            putExtra(ReminderReceiver.EXTRA_TEXT, text)
        }
        return PendingIntent.getBroadcast(this, id.hashCode(), intent, flags)
    }

    private fun scheduleAlarmInternal(id: String, triggerAtMillis: Long, title: String, text: String): String {
        ReminderReceiver.ensureChannel(this)
        val am = getSystemService(AlarmManager::class.java) ?: return "error:no_alarm_manager"
        val pi = reminderPendingIntent(
            id, title, text,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        ) ?: return "error:no_pending_intent"

        return try {
            val canExact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()
            if (canExact) {
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            } else {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            }
            Log.i(TAG, "reminder scheduled: $id at $triggerAtMillis (exact=$canExact)")
            "scheduled"
        } catch (se: SecurityException) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            Log.w(TAG, "exact alarm denied, used inexact for $id", se)
            "scheduled"
        } catch (e: Exception) {
            Log.w(TAG, "scheduleReminder failed for $id", e)
            "error:${e.javaClass.simpleName}"
        }
    }

    fun cancelReminder(id: String): Boolean {
        if (id.isBlank()) return false
        val am = getSystemService(AlarmManager::class.java)
        val pi = reminderPendingIntent(
            id, "", "",
            PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE
        )
        var cancelled = false
        if (pi != null && am != null) {
            am.cancel(pi)
            pi.cancel()
            cancelled = true
        }
        getSystemService(NotificationManager::class.java)?.cancel(id.hashCode())
        Log.i(TAG, "reminder cancelled: $id (existed=$cancelled)")
        return true
    }

    override fun onPause() {
        super.onPause()
        pendingLaunch = null
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onDestroy() {
        pendingFilePathCallback?.onReceiveValue(null)
        pendingFilePathCallback = null
        pendingExportJson = null
        pendingReminder = null
        pendingGeo = null
        handler.removeCallbacksAndMessages(null)
        root.removeView(webView)
        webView.destroy()
        super.onDestroy()
    }
}
