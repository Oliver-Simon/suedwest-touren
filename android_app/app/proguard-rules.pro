# JavaScript bridge methods are called reflectively by the WebView
-keepclassmembers class de.oliver.suedwesttouren.NativeBridge {
    @android.webkit.JavascriptInterface <methods>;
}
