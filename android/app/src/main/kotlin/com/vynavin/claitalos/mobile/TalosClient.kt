package com.vynavin.claitalos.mobile

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal HTTP client for the TALOS mobile API. No external dependencies —
 * HttpURLConnection + org.json. All calls run on Dispatchers.IO.
 *
 * The server contract (src/mobile_api.py):
 *   GET  /api/mobile/status      -> {ok, bot_name, ...}
 *   POST /api/mobile/message     -> 202 {ok, run_id}   (submit-then-stream)
 *   GET  /api/mobile/events      -> {ok, events:[{id,...}], last_id} (long-poll)
 *   POST /api/mobile/events/ack  -> {ok, acked}
 */
class TalosClient(private val baseUrl: String, private val token: String) {

    sealed class ApiResult<out T> {
        data class Ok<T>(val value: T) : ApiResult<T>()
        sealed class Err : ApiResult<Nothing>() {
            data object Unauthorized : Err()
            data object HttpsRequired : Err()
            data object RateLimited : Err()
            data object NotFound : Err()
            data class Server(val code: Int, val body: String) : Err()
            data class Network(val cause: IOException) : Err()
        }
    }

    data class Status(
        val botName: String,
        val telegramConfigured: Boolean,
        val modelConfigured: Boolean,
        val glassesModels: List<String>,
        val glassesModel: String,
        val speakEnabled: Boolean,
    )

    data class Submitted(val runId: String)

    data class MobileEvent(
        val id: Long,
        val runId: String,
        val type: String,
        val text: String,
        val speakText: String,
        val speak: Boolean,
    )

    data class Events(val events: List<MobileEvent>, val lastId: Long)

    suspend fun status(): ApiResult<Status> = request("GET", "/api/mobile/status", null) { body ->
        val obj = JSONObject(body)
        Status(
            botName = obj.optString("bot_name", "Clai"),
            telegramConfigured = obj.optBoolean("telegram_configured", false),
            modelConfigured = obj.optBoolean("model_configured", false),
            glassesModels = buildList {
                val arr = obj.optJSONArray("glasses_models")
                if (arr != null) for (i in 0 until arr.length()) add(arr.optString(i))
            },
            glassesModel = obj.optString("glasses_model", ""),
            speakEnabled = obj.optBoolean("speak_enabled", true),
        )
    }

    suspend fun sendMessage(text: String, glassesModel: String?, origin: String): ApiResult<Submitted> {
        val payload = JSONObject().apply {
            put("text", text)
            put("origin", origin)
            if (!glassesModel.isNullOrBlank()) put("glasses_model", glassesModel)
        }
        return request("POST", "/api/mobile/message", payload) { body ->
            Submitted(JSONObject(body).optString("run_id", ""))
        }
    }

    suspend fun pollEvents(since: Long, waitSeconds: Int = 25): ApiResult<Events> {
        val path = "/api/mobile/events?since=$since&wait=$waitSeconds"
        return request("GET", path, null) { body ->
            val obj = JSONObject(body)
            val arr = obj.optJSONArray("events")
            val events = buildList {
                if (arr != null) {
                    for (i in 0 until arr.length()) {
                        val e = arr.optJSONObject(i) ?: continue
                        add(
                            MobileEvent(
                                id = e.optLong("id", 0L),
                                runId = e.optString("run_id", ""),
                                type = e.optString("type", "text"),
                                text = e.optString("text", ""),
                                speakText = e.optString("speak_text", ""),
                                speak = e.optInt("speak", 0) == 1,
                            )
                        )
                    }
                }
            }
            Events(events, obj.optLong("last_id", since))
        }
    }

    suspend fun ackEvents(lastId: Long): ApiResult<Unit> {
        val payload = JSONObject().put("last_id", lastId)
        return request("POST", "/api/mobile/events/ack", payload) { }
    }

    private suspend fun <T> request(
        method: String,
        path: String,
        payload: JSONObject?,
        parse: (String) -> T,
    ): ApiResult<T> = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL("$baseUrl$path").openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 10_000
                readTimeout = if (path.startsWith("/api/mobile/events")) {
                    (waitOf(path) + 15) * 1000
                } else {
                    30_000
                }
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Accept", "application/json")
                doInput = true
                if (payload != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                }
            }
            if (payload != null) {
                conn.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            when {
                code in 200..299 -> ApiResult.Ok(parse(body))
                code == 401 -> ApiResult.Err.Unauthorized
                code == 403 || code == 426 -> ApiResult.Err.HttpsRequired
                code == 429 -> ApiResult.Err.RateLimited
                code == 404 -> ApiResult.Err.NotFound
                else -> ApiResult.Err.Server(code, body.take(300))
            }
        } catch (e: IOException) {
            ApiResult.Err.Network(e)
        } catch (e: Exception) {
            ApiResult.Err.Server(-1, (e.message ?: "parse error").take(300))
        } finally {
            conn?.disconnect()
        }
    }

    private fun waitOf(path: String): Int =
        Regex("wait=(\\d+)").find(path)?.groupValues?.get(1)?.toIntOrNull() ?: 25
}

/**
 * Cursor bookkeeping for the events long-poll. Pure logic, unit-tested:
 * the cursor only ever moves forward, so a reconnect mid-run resumes
 * exactly where it left off and never misses a spoken reply.
 */
class EventsCursor(initial: Long = 0L) {
    var lastId: Long = initial
        private set

    /** Apply a batch; returns true if the cursor advanced. */
    fun apply(events: List<TalosClient.MobileEvent>): Boolean {
        val max = events.maxOfOrNull { it.id } ?: return false
        if (max <= lastId) return false
        lastId = max
        return true
    }
}
