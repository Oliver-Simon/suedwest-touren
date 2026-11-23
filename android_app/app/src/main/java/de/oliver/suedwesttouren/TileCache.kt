package de.oliver.suedwesttouren

import android.content.Context
import android.util.Log
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import java.io.ByteArrayInputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Disk cache for map tiles (OSM / OpenTopoMap / Waymarked Trails): every tile seen once stays available offline.
 * Keeps at most MAX_TILES files (oldest removed first). Responses carry CORS headers because the web UI
 * requests tiles with crossOrigin="anonymous".
 */
class TileCache(context: Context) {

    companion object {
        private val HOSTS = setOf(
            "tile.openstreetmap.org",
            "a.tile.opentopomap.org",
            "b.tile.opentopomap.org",
            "c.tile.opentopomap.org",
            "tile.waymarkedtrails.org"
        )
        private const val MAX_TILES = 4000
        private const val USER_AGENT = "SuedwestTouren/1.0 (Android; +https://oliver-simon.github.io/suedwest-touren/)"

        fun isTileHost(host: String?): Boolean = host != null && host in HOSTS
    }

    private val dir = File(context.cacheDir, "tiles").apply { mkdirs() }
    @Volatile private var writes = 0

    fun load(request: WebResourceRequest): WebResourceResponse {
        val url = request.url
        val file = File(dir, (url.host + url.path).replace(Regex("[^A-Za-z0-9._-]"), "_"))
        if (file.length() > 0) {
            file.setLastModified(System.currentTimeMillis())
            return response(file.readBytes())
        }
        return try {
            val conn = (URL(url.toString()).openConnection() as HttpURLConnection).apply {
                connectTimeout = 8000
                readTimeout = 12000
                setRequestProperty("User-Agent", USER_AGENT)
            }
            conn.inputStream.use { input ->
                val bytes = input.readBytes()
                if (conn.responseCode == 200 && bytes.isNotEmpty()) {
                    file.writeBytes(bytes)
                    if (++writes % 100 == 0) trim()
                }
                response(bytes)
            }
        } catch (e: Exception) {
            Log.d(MainActivity.TAG, "tile offline: ${url.path}")
            WebResourceResponse("image/png", null, 504, "Offline", cors(), ByteArrayInputStream(ByteArray(0)))
        }
    }

    private fun cors() = mapOf("Access-Control-Allow-Origin" to "*", "Cache-Control" to "max-age=604800")

    private fun response(bytes: ByteArray) =
        WebResourceResponse("image/png", null, 200, "OK", cors(), ByteArrayInputStream(bytes))

    private fun trim() {
        val files = dir.listFiles() ?: return
        if (files.size <= MAX_TILES) return
        files.sortedBy { it.lastModified() }.take(files.size - MAX_TILES).forEach { it.delete() }
    }
}
