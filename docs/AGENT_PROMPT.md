# Prompt für Antigravity / Gemini CLI (eine Phase pro Sitzung)

Im Projektordner starten (`cd ~/suedwest_touren`), dann diesen Text einfügen und `<N>` durch die Phasennummer ersetzen:

```text
Du arbeitest im Repo /home/oliver/suedwest_touren (Südwest-Touren: PWA + Android-App aus EINER Oberfläche).

Lies zuerst vollständig: docs/REDESIGN_PLAN.md, android_app/README.md, PROJECT.md (Abschnitt v4)
und template.html. Setze danach AUSSCHLIESSLICH Phase <N> aus docs/REDESIGN_PLAN.md um.

Harte Regeln:
1. Ändere nur template.html (und falls die Phase es verlangt sw.template.js bzw. android_app/).
   NIEMALS data/, tests/, planner.py, planer/ oder img/ anfassen. Keine Fahrpläne, Zeiten,
   Haltestellen oder Kennzahlen erfinden – alle Inhalte kommen aus DATA über die vorhandenen
   Funktionen (getSelectedWindow, getPlan, hikePolyPts, build*Url, tourMiddle).
2. Halte alle Leitplanken aus Abschnitt „Leitplanken“ ein (IDs, Button-Texte, Reihenfolgen,
   alle 150 Karten im DOM). Keine externen Bibliotheken oder CDNs.
3. Nach der Umsetzung ausführen und Ausgabe zeigen:
     python3 build_pwa.py
     python3 tests/run_e2e.py --tier all --target local
   Erwartet: "SUMMARY: total=289 passed=280 failed=0 errors=0 skipped_local=9".
   Schlägt ein Test fehl: Ursache in template.html beheben – Tests niemals ändern oder überspringen.
4. Erst wenn alles grün ist: git add -A && git commit -m "feat(redesign): Phase <N> – <Kurzbeschreibung>".
   NICHT pushen.
5. Zum Schluss kurz berichten: was geändert wurde, Testergebnis, offene Punkte.
   Keine weiteren Phasen beginnen.

Optional am Handy prüfen (ADB-Gerät 320437156021):
  cd android_app && ./gradlew assembleDebug && adb install -r app/build/outputs/apk/debug/app-debug.apk
  adb shell am start -n de.oliver.suedwesttouren/.MainActivity
  adb exec-out screencap -p > /tmp/shot.png
Vor jedem Tippen prüfen, welche App vorne ist (adb shell dumpsys window | grep mCurrentFocus);
nie "input keyevent BACK" verwenden, um die App zu verlassen.
```

Nach jeder Phase selbst kurz am Handy anschauen; passt etwas nicht: `git reset --hard HEAD~1` macht die Phase rückgängig.
Live stellen: `git push origin main` (GitHub Pages aktualisiert sich nach ca. 1 Minute).
