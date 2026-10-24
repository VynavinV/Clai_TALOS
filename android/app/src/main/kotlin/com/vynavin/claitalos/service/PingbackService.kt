package com.vynavin.claitalos.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.vynavin.claitalos.GlassesApp
import com.vynavin.claitalos.MainActivity
import com.vynavin.claitalos.R
import com.vynavin.claitalos.dat.GlassesConnection
import com.vynavin.claitalos.mobile.EventsCursor
import com.vynavin.claitalos.mobile.Prefs
import com.vynavin.claitalos.mobile.TalosClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * The long-run "ping the glasses" bridge.
 *
 * Started when a glasses-originated run is submitted (202 + run_id). Keeps a
 * long-poll on /api/mobile/events open with auto-reconnect and cursor resume,
 * so network drops, phone lock, and app restarts during a 10–20 minute run
 * are all recoverable — the server queue replays undelivered speak events.
 *
 * Each speak=true event is spoken through the glasses (TTS over BT SCO/HFP).
 * If the glasses session is down when a reply arrives, a high-priority
 * notification is shown instead and the event stays queued for re-speak on
 * reconnect (simplest reliable fallback: the event replays because the cursor
 * only advances on ack, and we ack only after speaking or notifying).
 *
 * The service stops after the run's run_done event is acked (or on Stop).
 */
class PingbackService : Service() {

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var pollJob: Job? = null
    private lateinit var prefs: Prefs
    private var speechOut: SpeechOut? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val demoDelaySec = intent?.getIntExtra(EXTRA_DEMO_DELAY_SEC, -1) ?: -1
        val stop = intent?.getBooleanExtra(EXTRA_STOP, false) ?: false
        if (stop) {
            stopSelf()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, buildNotification("Clai is working…"))

        if (demoDelaySec >= 0) {
            startDemo(demoDelaySec)
        } else {
            startPolling()
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        serviceScope.cancel()
        speechOut?.shutdown()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ real

    private fun startPolling() {
        if (pollJob?.isActive == true) return
        pollJob = serviceScope.launch {
            val client = TalosClient(prefs.serverUrl, prefs.token)
            val cursor = EventsCursor(prefs.eventCursor)
            var backoffMs = INITIAL_BACKOFF_MS
            var sawRunDone = false

            while (isActive && !sawRunDone) {
                val result = client.pollEvents(cursor.lastId, waitSeconds = POLL_WAIT_SEC)
                when (result) {
                    is TalosClient.ApiResult.Ok -> {
                        backoffMs = INITIAL_BACKOFF_MS
                        val events = result.value.events
                        if (events.isNotEmpty()) {
                            handleEvents(events)
                            cursor.apply(events)
                            prefs.eventCursor = cursor.lastId
                            // Ack after handling (speak or notify), so an
                            // interrupt mid-batch replays instead of losing it.
                            client.ackEvents(cursor.lastId)
                            if (events.any { it.type == "run_done" }) {
                                sawRunDone = true
                            }
                        }
                    }
                    is TalosClient.ApiResult.Err.Unauthorized -> {
                        updateNotification("Clai: token rejected — re-pair in the app.")
                        break
                    }
                    is TalosClient.ApiResult.Err.Network -> {
                        // Auto-reconnect with modest backoff; queue replays.
                        updateNotification("Clai: reconnecting…")
                        delay(backoffMs)
                        backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
                    }
                    is TalosClient.ApiResult.Err.HttpsRequired -> {
                        updateNotification("Clai: server requires HTTPS (use your Tailscale URL).")
                        delay(10_000)
                    }
                    is TalosClient.ApiResult.Err.RateLimited -> delay(10_000)
                    is TalosClient.ApiResult.Err.NotFound,
                    is TalosClient.ApiResult.Err.Server,
                    -> {
                        updateNotification("Clai: server error — retrying.")
                        delay(backoffMs)
                        backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
                    }
                }
            }

            if (sawRunDone) {
                stopSelf()
            }
        }
    }

    private suspend fun handleEvents(events: List<TalosClient.MobileEvent>) {
        for (event in events) {
            if (event.type == "run_done" || event.type == "status") continue
            if (!event.speak || !prefs.speakFollowUps) continue
            val text = event.speakText.ifBlank { event.text }
            if (text.isBlank()) continue
            val connected = (applicationContext as? GlassesApp)
                ?.connection?.connState?.value == GlassesConnection.ConnState.CONNECTED
            if (connected) {
                speak(text)
            } else {
                notifyReply(text)
            }
        }
    }

    private suspend fun speak(text: String) {
        if (!prefs.speakReplies) return
        updateNotification("Clai is speaking…")
        val out = speechOut ?: SpeechOut(this).also { speechOut = it }
        out.speak(text)
        updateNotification("Clai is working…")
    }

    private fun notifyReply(text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val open = PendingIntent.getActivity(
            this, 1,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Clai replied (glasses offline)")
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(open)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        try {
            nm.notify(REPLY_NOTIFICATION_ID, notification)
        } catch (e: SecurityException) {
            Log.w(TAG, "notification needs POST_NOTIFICATIONS permission", e)
        }
    }

    // ------------------------------------------------------------------ demo

    /** Fake slow-server mode: speak a canned reply after a delay — tests the
     * TTS + SCO + notification path without hardware or a real run. */
    private fun startDemo(delaySec: Int) {
        serviceScope.launch {
            updateNotification("Clai (demo): reply in ${delaySec}s…")
            delay(delaySec * 1000L)
            speak("Done! Your slideshow about France is ready. The link is on Telegram.")
            stopSelf()
        }
    }

    // ----------------------------------------------------------- notifications

    private fun createChannel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                getString(R.string.pingback_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stopIntent = PendingIntent.getService(
            this, 2,
            Intent(this, PingbackService::class.java).putExtra(EXTRA_STOP, true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(getString(R.string.pingback_running_detail)))
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "Stop listening for replies", stopIntent)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        try {
            nm.notify(NOTIFICATION_ID, buildNotification(text))
        } catch (_: Exception) {
        }
    }

    companion object {
        private const val TAG = "ClaiPingback"
        private const val CHANNEL_ID = "clai_pingback"
        private const val NOTIFICATION_ID = 1001
        private const val REPLY_NOTIFICATION_ID = 1002
        private const val EXTRA_STOP = "stop"
        private const val EXTRA_DEMO_DELAY_SEC = "demo_delay_sec"
        private const val POLL_WAIT_SEC = 25
        private const val INITIAL_BACKOFF_MS = 2_000L
        private const val MAX_BACKOFF_MS = 30_000L

        /** Start the ping-back bridge for a just-submitted run. */
        fun start(context: Context) {
            val intent = Intent(context, PingbackService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /** Debug/demo: speak a canned reply after [delaySec]. */
        fun startDemo(context: Context, delaySec: Int) {
            val intent = Intent(context, PingbackService::class.java)
                .putExtra(EXTRA_DEMO_DELAY_SEC, delaySec)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, PingbackService::class.java))
        }
    }
}
