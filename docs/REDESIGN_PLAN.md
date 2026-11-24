# Redesign v5 – Umsetzungsplan (Übergabe an einen Coding-Agenten)

Vorlage: Redesign-Konzept (6 Screens) https://claude.ai/artifact/G3dYAHP19gTVAPRBdbhCFG
Stand des Codes: v4 (`7e379ff`), alle E2E-Tests grün.

## Grundsatz: eine Oberfläche für alles

Die UI lebt **nur in `template.html`**. `python3 build_pwa.py` baut daraus PWA (`index.html`), Standalone-Datei
(`/home/oliver/suedwest_touren.html`) und die Assets der Android-App (`android_app/app/src/main/assets/www/`).
Das Redesign wird deshalb **schrittweise in `template.html`** umgesetzt – kein Neuschreiben in Kotlin/Compose nötig.
Die native Brücke (`window.SWNative`, siehe `android_app/README.md`) bleibt unverändert nutzbar.

## Leitplanken (sonst brechen Tests oder Daten)

1. `data/data.json`, `data/tours_catalog*.json` und `tests/` **nicht ändern**.
2. Nach **jedem** Schritt: `python3 build_pwa.py` und danach mindestens
   `python3 tests/run_e2e.py --tier 1 --target local` (alle Tiers vor dem Commit, ~20 min). Erwartung: 280 passed, 0 failed.
3. Was die Tests im DOM erwarten (Playwright, 412×915, deutscher Text):
   - alle 150 Touren als `#list .card` im DOM (keine Virtualisierung), Klick öffnet `#detail`, `#backBtn` schließt es;
   - sichtbare Buttons mit den Texten „Alle“, „Meine Mapy“, „Neue Entdeckungen“, „Pfälzerwald“, „Schwarzwald“,
     „Odenwald“, „Abendsonne“, „Heute noch machbar“ (Klick darauf blendet `#feasibleTime` ein), Suchfeld `#q`;
   - `#langBtn`, `#themeBtn`, `#homeEditBtn` + `#homeModal`, Basis-Buttons mit „Jugenheim“/„Karlsruhe“, Tage „Mo–Fr/Sa/So“;
   - in der Detailansicht: erster Button/Link mit „GPX“ löst einen Download aus; der erste `bahn.de`-Link hat ein
     `#sts=true&so=…&zo=…&hd=…`-Fragment; Google-Maps- und `muenchennavigator://`-Links vorhanden;
     Abfahrts-Buttons (`.win-btn[data-win]`, Text mit HH:MM) stehen vor allen anderen HH:MM-Buttons, und der zweite davon
     ist **nicht** der vorausgewählte;
   - Karte: `#layerBtn` (Text „Layer“), `#fitBtn`, `#fullBtn`, `#miniBtn`, `#gpsBtn`, Leaflet-Pfade im `.leaflet-overlay-pane`;
   - `sw.js` mit `SHELL = [...]`, `MAX_TILES = 3000`, `tile.openstreetmap.org`; keine Konsolenfehler.
4. Datenzugriff nur über die vorhandenen Funktionen `getSelectedWindow()`, `getPlan()`, `hikePolyPts()`,
   `buildDbUrl()/buildDbAppUrl()/buildMvvAppUrl()/buildNvvAppUrl()/buildMyVrnUrl()/buildMapyRouteUrl()`.
5. Keine neuen externen Abhängigkeiten/CDNs (offline-fähig, Leaflet ist inline).

## Phasen (je eine Sitzung, je ein Commit)

**Phase 1 – Design-Tokens & Typo.** CSS-Variablen auf die Konzeptfarben umstellen (Grund `#F3F5F1`, Text `#13201C`,
Primär `#0F4C5C`, Akzent `#F59E0B`, Regionen Pfalz `#C2410C`, Odenwald `#15803D`, Schwarzwald `#6D28D9`), Radien 14–20 px,
Inter/Space Grotesk als lokale Fallback-Stacks (keine Webfonts laden). Dark-Mode-Pendants ergänzen.
*Fertig, wenn:* Tests grün, Light/Dark am Handy geprüft.

**Phase 2 – Untere Navigation & Startseite „Entdecken“.** Feste Bottom-Nav (Entdecken · Karte · Gemerkt · Unterwegs) als
Ansichten innerhalb derselben Seite. „Heute empfohlen“-Karte (erste machbare Tour) + horizontale Reihe „Weitere für heute“
oberhalb der bestehenden Regionsliste. Filter-Pills bleiben sichtbar.
*Fertig, wenn:* Tests grün, erste Tour ohne Scrollen sichtbar.

**Phase 3 – Kartenansicht mit Bottom-Sheet.** Leaflet-Karte in der Ansicht „Karte“ bildschirmfüllend, darunter ein
Sheet mit den Touren im Kartenausschnitt (`map.getBounds()`), Tippen auf Pin hebt die Karte im Sheet hervor.

**Phase 4 – Tourseite.** Kennzahl-Kacheln (Anfahrt, Wanderung, Höhenmeter, Vor Ort), vorhandenes Höhenprofil,
kompakter Tagesablauf (Abfahrt → Ankunft → Wanderung → Rückfahrt) statt der langen Timeline (Timeline als „Details“
ausklappbar lassen), feste Aktionsleiste unten: „Verbindung“ + „In Mapy öffnen“.

**Phase 5 – Tagesplan.** Zeitleiste 05–24 Uhr mit Blöcken Hinfahrt/Vor Ort/Rückfahrt, Auswahl über die bestehenden
`.win-btn`/`.stay-btn`/`.ret-btn` (IDs und Reihenfolge beibehalten, siehe Leitplanke 3).

**Phase 6 – App-Auswahl als Sheet.** Tabelle „App übernimmt Start/Ziel/Vias/Zeit“, Auswahl merken (`localStorage`).

**Phase 7 – Unterwegs-Modus** (nur App/PWA): gewählte Tour „starten“, Countdown bis zum Aufbruch,
spätere Rückfahrten, Erinnerung (in der App über eine neue Bridge-Methode mit `AlarmManager`/Notification).

**Phase 8 – Gemerkt.** Merkliste in `localStorage` (`sw:favs`), Export/Import als JSON.

## Prüfen am Handy (ADB, Gerät `320437156021`)

```bash
cd android_app && ./gradlew assembleDebug && adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n de.oliver.suedwesttouren/.MainActivity
adb exec-out screencap -p > shot.png ; adb logcat -d -s SuedwestTouren:*
```

Fallstricke: Handy sperrt nach dem Bildschirmschoner (Muster) – vorher `adb shell svc power stayon usb`, danach
`adb shell settings put global stay_on_while_plugged_in 0`. Vor jedem Tap prüfen, welche App vorne ist
(`adb shell dumpsys window | grep mCurrentFocus`), nie per `input keyevent BACK` aus der App heraus navigieren.
