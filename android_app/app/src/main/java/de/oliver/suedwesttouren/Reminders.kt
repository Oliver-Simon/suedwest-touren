package de.oliver.suedwesttouren

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.util.Log
import org.json.JSONObject

/**
 * Reminder scheduling shared by the activity (via the JS bridge) and [BootReceiver].
 *
 * Every scheduled reminder is also persisted in SharedPreferences, because the system drops all
 * alarms on reboot (and on app updates). [rescheduleAll] re-arms the future ones afterwards.
 * Exact timing: SCHEDULE_EXACT_ALARM (Android 12/12L, auto-granted) and USE_EXACT_ALARM (Android 13+,
 * auto-granted) – the inexact fallback only remains for the case the user revokes the permission.
 */
object Reminders {
    private const val PREFS = "reminders"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun pendingIntent(ctx: Context, id: String, title: String, text: String, flags: Int): PendingIntent? {
        val intent = Intent(ctx, ReminderReceiver::class.java).apply {
            action = ReminderReceiver.ACTION_REMINDER
            data = Uri.parse("swreminder://$id")
            putExtra(ReminderReceiver.EXTRA_ID, id)
            putExtra(ReminderReceiver.EXTRA_TITLE, title)
            putExtra(ReminderReceiver.EXTRA_TEXT, text)
        }
        return PendingIntent.getBroadcast(ctx, id.hashCode(), intent, flags)
    }

    /** Arms the alarm and remembers it. Returns "scheduled" or "error:…". */
    fun schedule(ctx: Context, id: String, triggerAtMillis: Long, title: String, text: String): String {
        val res = arm(ctx, id, triggerAtMillis, title, text)
        if (res == "scheduled") {
            val json = JSONObject().put("at", triggerAtMillis).put("title", title).put("text", text)
            prefs(ctx).edit().putString(id, json.toString()).apply()
        }
        return res
    }

    private fun arm(ctx: Context, id: String, triggerAtMillis: Long, title: String, text: String): String {
        ReminderReceiver.ensureChannel(ctx)
        val am = ctx.getSystemService(AlarmManager::class.java) ?: return "error:no_alarm_manager"
        val pi = pendingIntent(ctx, id, title, text, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            ?: return "error:no_pending_intent"
        return try {
            val canExact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()
            if (canExact) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            Log.i(MainActivity.TAG, "reminder scheduled: $id at $triggerAtMillis (exact=$canExact)")
            "scheduled"
        } catch (se: SecurityException) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pi)
            Log.w(MainActivity.TAG, "exact alarm denied, used inexact for $id", se)
            "scheduled"
        } catch (e: Exception) {
            Log.w(MainActivity.TAG, "scheduleReminder failed for $id", e)
            "error:${e.javaClass.simpleName}"
        }
    }

    fun cancel(ctx: Context, id: String): Boolean {
        forget(ctx, id)
        val am = ctx.getSystemService(AlarmManager::class.java)
        val pi = pendingIntent(ctx, id, "", "", PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE)
        if (pi != null && am != null) {
            am.cancel(pi)
            pi.cancel()
            return true
        }
        return false
    }

    fun forget(ctx: Context, id: String) {
        prefs(ctx).edit().remove(id).apply()
    }

    /** Re-arms all remembered reminders that still lie in the future; drops the rest. */
    fun rescheduleAll(ctx: Context): Int {
        val now = System.currentTimeMillis()
        val p = prefs(ctx)
        var n = 0
        for ((id, raw) in p.all) {
            val json = runCatching { JSONObject(raw as String) }.getOrNull()
            val at = json?.optLong("at") ?: 0L
            if (json == null || at <= now) {
                p.edit().remove(id).apply()
                continue
            }
            if (arm(ctx, id, at, json.optString("title"), json.optString("text")) == "scheduled") n++
        }
        Log.i(MainActivity.TAG, "reminders re-armed after boot/update: $n")
        return n
    }
}

/** Alarms do not survive a reboot or an app update – re-arm the remembered reminders. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON" -> Reminders.rescheduleAll(context)
        }
    }
}
