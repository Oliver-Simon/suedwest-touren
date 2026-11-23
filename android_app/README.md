# Südwest-Touren – native Android-App

Native Kotlin-App (`de.oliver.suedwesttouren`) mit lokaler Asset-WebView und Kotlin-Bridge.
Die Oberfläche ist dieselbe wie in der PWA (`../template.html`), läuft aber komplett aus dem APK:
`assets/www/index.html` (Daten inline, ~4,7 MB) + `assets/www/img/*.webp` (150 Fotos).

## Bauen & installieren

```bash
cd /home/oliver/suedwest_touren
python3 build_pwa.py                 # erzeugt index.html, sw.js, Standalone-HTML UND android_app/app/src/main/assets/www/
cd android_app
./gradlew assembleDebug              # → app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Was die App nativ macht

| Funktion | Umsetzung |
|---|---|
| Sofortstart | Assets über `WebViewAssetLoader` (`https://appassets.androidplatform.net/assets/www/`), kein Netz/Browser-Cache; System-Splash bleibt bis zum ersten Render (`SWNative.contentReady`). Gemessen: ~0,8 s ab Prozessstart. |
| App-Links | `shouldOverrideUrlLoading` → `Intent.parseUri(…, URI_INTENT_SCHEME)` + `startActivity`: DB Navigator (`dbnav://…`, `de.hafas.android.db`), München Navigator (`muenchennavigator://query?S&Z&V1&V2&date&time`, `de.hafas.android.sbm`), NVV (`nvv://query?…`, `de.hafas.android.nvv`), myVRN (`geo:` an `com.mdv.VRNCompanion`), Mapy (`https://mapy.com/fnc/v1/…` an `cz.seznam.mapy`). Fehlt eine App, wird `browser_fallback_url` geöffnet. |
| GPX → Mapy | `SWNative.openGpx()` schreibt die Datei nach `cacheDir/gpx/`, `FileProvider` (`${applicationId}.files`) + `ACTION_VIEW` `application/gpx+xml` + `FLAG_GRANT_READ_URI_PERMISSION` direkt an `cz.seznam.mapy` – ohne Download-Ordner. |
| Zurück-Geste | `OnBackPressedCallback` → `window.swHandleBack()` schließt Dialog → Vollbildkarte → Detailansicht; erst auf der Startseite beendet „Zurück“ die App. |
| Offline-Karte | `TileCache`: jede gesehene OSM-/OpenTopoMap-/Waymarked-Kachel landet in `cacheDir/tiles` (max. 4000) und ist danach offline verfügbar. |
| Theme | DayNight-Theme; Status-/Navigationsleiste folgen dem In-App-Theme (`SWNative.setDarkTheme`). Edge-to-Edge-Insets (Android 15) inkl. Tastatur. |
| Standort | `navigator.geolocation` über `WebChromeClient` + Laufzeitberechtigung. |

Hinweis myVRN: Die App nimmt laut Test nur ein Ziel per `geo:` an (Route ab aktuellem Standort);
`vrn://` übernimmt keine Start/Ziel-Parameter. Für Start + Vias + Uhrzeit sind MVV und NVV da.
