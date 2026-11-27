package de.oliver.suedwesttouren

import android.content.Context
import android.util.Log
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.io.File
import java.net.Inet4Address
import java.util.concurrent.TimeUnit

/**
 * Disk cache for map tiles (OSM / OpenTopoMap / Waymarked Trails / OpenRailwayMap / ÖPNV-Karte): every tile seen once stays available offline.
 * Keeps at most MAX_TILES files (oldest removed first). Responses carry CORS headers because the web UI
 * requests tiles with crossOrigin="anonymous".
 *
 * Fetches go through OkHttp with IPv4 addresses first: on networks that hand out an IPv6 address without
 * working IPv6 routing, the platform HttpURLConnection tried every AAAA record with the full connect
 * timeout (> 1 min per tile) while the WebView itself falls back to IPv4 at once.
 */
class TileCache(context: Context) {

    companion object {
        private val HOSTS = setOf(
            "tile.openstreetmap.org",
            "a.tile.opentopomap.org",
            "b.tile.opentopomap.org",
            "c.tile.opentopomap.org",
            "tile.waymarkedtrails.org",
            "a.tiles.openrailwaymap.org",
            "b.tiles.openrailwaymap.org",
            "c.tiles.openrailwaymap.org",
            "tile.memomaps.de",
            "a.tile-cyclosm.openstreetmap.fr",
            "b.tile-cyclosm.openstreetmap.fr",
            "c.tile-cyclosm.openstreetmap.fr",
            "server.arcgisonline.com"
        )
        private const val MAX_TILES = 4000
        private const val USER_AGENT = "SuedwestTouren/1.0 (Android; +https://oliver-simon.github.io/suedwest-touren/)"

        fun isTileHost(host: String?): Boolean = host != null && host in HOSTS
    }

    private val dir = File(context.cacheDir, "tiles").apply { mkdirs() }
    private val client = OkHttpClient.Builder()
        .dns(object : Dns {
            override fun lookup(hostname: String) =
                Dns.SYSTEM.lookup(hostname).sortedBy { if (it is Inet4Address) 0 else 1 }
        })
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .build()
    @Volatile private var writes = 0

    fun load(request: WebResourceRequest): WebResourceResponse {
        val url = request.url
        val file = File(dir, (url.host + url.path).replace(Regex("[^A-Za-z0-9._-]"), "_"))
        if (file.length() > 0) {
            file.setLastModified(System.currentTimeMillis())
            return response(file.readBytes())
        }
        return try {
            val req = Request.Builder()
                .url(url.toString())
                .header("User-Agent", USER_AGENT)
                // OpenRailwayMap answers 403 without a Referer (tile usage policy)
                .header("Referer", request.requestHeaders["Referer"] ?: "https://appassets.androidplatform.net/")
                .build()
            client.newCall(req).execute().use { resp ->
                val bytes = resp.body?.bytes() ?: ByteArray(0)
                if (resp.code != 200 || bytes.isEmpty()) throw java.io.IOException("HTTP ${resp.code}")
                file.writeBytes(bytes)
                if (++writes % 100 == 0) trim()
                response(bytes)
            }
        } catch (e: Exception) {
            Log.d(MainActivity.TAG, "tile offline: ${url.host}${url.path} $e")
            WebResourceResponse("image/png", null, 504, "Offline", cors(), ByteArrayInputStream(ByteArray(0)))
        }
    }

    private fun cors() = mapOf("Access-Control-Allow-Origin" to "*", "Cache-Control" to "max-age=604800")

    private fun response(bytes: ByteArray): WebResourceResponse {
        // Esri serves JPEG tiles, the others PNG
        val jpeg = bytes.size > 2 && bytes[0] == 0xFF.toByte() && bytes[1] == 0xD8.toByte()
        return WebResourceResponse(if (jpeg) "image/jpeg" else "image/png", null, 200, "OK", cors(), ByteArrayInputStream(bytes))
    }

    private fun trim() {
        val files = dir.listFiles() ?: return
        if (files.size <= MAX_TILES) return
        files.sortedBy { it.lastModified() }.take(files.size - MAX_TILES).forEach { it.delete() }
    }
}
