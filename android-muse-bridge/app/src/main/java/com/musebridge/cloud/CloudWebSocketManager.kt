package com.musebridge.cloud

import android.util.Log
import kotlinx.coroutines.*
import okhttp3.*
import okio.Buffer
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Cloud WebSocket client for streaming raw Muse BLE data to the cloud server.
 *
 * Features:
 *   - OkHttp WebSocket (persistent connection)
 *   - Binary framing with session_id + sequence numbers
 *   - JSON text frames for control/heartbeat messages
 *   - In-memory buffer for brief disconnections (~3000 packets)
 *   - Automatic reconnection with exponential backoff
 *   - Heartbeat keep-alive every 5 seconds
 *
 * Protocol:
 *   Binary frame:
 *     Byte 0:       0x01 (sensor data)
 *     Bytes 1-16:   session_id (UTF-8, space-padded)
 *     Bytes 17-20:  seq_num (uint32 big-endian)
 *     Bytes 21+:    raw BLE payload bytes
 *
 *   Text frame (JSON):
 *     → {"type":"hello","device":"MuseS-XXXX","preset":"p1034"}
 *     ← {"type":"hello_ack","session_id":"abc123","server_time":"..."}
 *     → {"type":"heartbeat","battery":85}
 *     ← {"type":"pong"}
 *     ← {"type":"command","cmd":"halt"}
 */
class CloudWebSocketManager(
    private val scope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
) {
    companion object {
        private const val TAG = "CloudWS"
        private const val MAX_BUFFER_SIZE = 15_000      // ~2.5 min at 100 Hz
        private const val HEARTBEAT_INTERVAL_MS = 2_000L
        private const val RECONNECT_BASE_DELAY_MS = 1_000L
        private const val RECONNECT_MAX_DELAY_MS = 30_000L
    }

    // Configuration
    private var serverUrl: String = ""
    private var deviceName: String = "Unknown"
    private var preset: String = "p1034"

    // State
    @Volatile var isConnected: Boolean = false
        private set
    @Volatile var sessionId: String = ""
        private set
    @Volatile var packetCount: Long = 0
        private set
    @Volatile var dropCount: Long = 0
        private set

    private val started = AtomicBoolean(false)
    private val forcingReconnect = AtomicBoolean(false)
    private var webSocket: WebSocket? = null
    private var client: OkHttpClient? = null

    // Buffer for disconnected mode
    private val buffer = ConcurrentLinkedQueue<ByteArray>()
    private val seqNum = AtomicLong(0)

    // Jobs
    private var heartbeatJob: Job? = null
    private var reconnectJob: Job? = null
    private var reconnectAttempt: Int = 0

    // Callbacks (UI thread)
    var onConnected: (() -> Unit)? = null
    var onDisconnected: ((String) -> Unit)? = null   // reason
    var onSessionReady: ((String) -> Unit)? = null    // session_id
    var onCommand: ((String) -> Unit)? = null         // server command

    /**
     * Configure and start the WebSocket connection.
     */
    fun connect(serverUrl: String, deviceName: String = "Unknown", preset: String = "p1034") {
        if (this.serverUrl == serverUrl && isConnected) {
            Log.i(TAG, "Already connected to $serverUrl")
            return
        }

        this.serverUrl = serverUrl
        this.deviceName = deviceName
        this.preset = preset
        this.reconnectAttempt = 0

        if (started.getAndSet(true)) {
            // Already started — hard disconnect first
            disconnect()
            // The disconnect() will trigger auto-reconnect if started is true,
            // but we just set started to false in disconnect().
            // So we need to set started back to true and connect.
            started.set(true)
            delayStart(500)
        } else {
            doConnect()
        }
    }

    private fun doConnect() {
        if (serverUrl.isBlank()) {
            Log.e(TAG, "Server URL is blank, cannot connect")
            return
        }

        val client = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)  // No read timeout for streaming
            .pingInterval(30, TimeUnit.SECONDS)      // OkHttp-level ping
            .build()
        this.client = client

        val request = Request.Builder()
            .url(serverUrl)
            .build()

        Log.i(TAG, "Connecting to $serverUrl...")
        webSocket = client.newWebSocket(request, wsListener)
    }

    /**
     * Disconnect and clean up.
     */
    fun disconnect() {
        started.set(false)
        heartbeatJob?.cancel()
        reconnectJob?.cancel()

        try {
            // Use cancel() instead of close() for an immediate, hard disconnect.
            // This ensures the server sees the connection drop instantly.
            webSocket?.cancel()
        } catch (_: Exception) {}
        webSocket = null
        
        isConnected = false
        sessionId = ""
        Log.i(TAG, "Disconnected from cloud (hard cancel)")
    }

    /**
     * Enqueue a raw BLE notification payload for cloud upload.
     * @return true if sent immediately, false if buffered or dropped
     */
    fun sendPacket(data: ByteArray): Boolean {
        if (!isConnected || sessionId.isEmpty()) {
            bufferPacket(data)
            return false
        }

        drainBuffer()
        return sendBinaryFrame(data)
    }

    private fun bufferPacket(data: ByteArray) {
        while (buffer.size >= MAX_BUFFER_SIZE) {
            buffer.poll()
            dropCount++
        }
        buffer.offer(data)
    }

    private fun drainBuffer() {
        var drained = 0
        while (true) {
            val data = buffer.poll() ?: break
            if (!sendBinaryFrame(data)) {
                bufferPacket(data)
                break
            }
            drained++
        }
        if (drained > 0) {
            Log.i(TAG, "Drained $drained buffered packets")
        }
    }

    private fun sendBinaryFrame(data: ByteArray): Boolean {
        val seq = seqNum.incrementAndGet()

        // Build frame: [type:1][session_id:16][seq_num:4][payload:N]
        val sidBytes = sessionId.toByteArray(Charsets.UTF_8)
        val frame = ByteArray(21 + data.size)

        // Byte 0: frame type
        frame[0] = 0x01

        // Bytes 1-16: session_id (space-padded)
        for (i in 0 until 16) {
            frame[1 + i] = if (i < sidBytes.size) sidBytes[i] else ' '.code.toByte()
        }

        // Bytes 17-20: seq_num (uint32 big-endian)
        frame[17] = ((seq shr 24) and 0xFF).toByte()
        frame[18] = ((seq shr 16) and 0xFF).toByte()
        frame[19] = ((seq shr 8) and 0xFF).toByte()
        frame[20] = (seq and 0xFF).toByte()

        // Bytes 21+: payload
        System.arraycopy(data, 0, frame, 21, data.size)

        val ws = webSocket
        if (ws != null) {
            val bs = Buffer().also { it.write(frame) }.readByteString()
            val sent = ws.send(bs)
            if (sent) {
                packetCount++
                return true
            }
            Log.w(TAG, "ws.send() returned false — forcing reconnect")
            dropCount++
            forceReconnect("send buffer full")
            bufferPacket(data)
            return false
        }
        dropCount++
        return false
    }

    /**
     * Detect zombie WebSocket (connected flag set but socket dead) and reconnect.
     */
    private fun forceReconnect(reason: String) {
        if (!started.get()) return
        if (!forcingReconnect.compareAndSet(false, true)) return
        scope.launch {
            try {
                heartbeatJob?.cancel()
                isConnected = false
                sessionId = ""
                try { webSocket?.cancel() } catch (_: Exception) {}
                webSocket = null
                onDisconnected?.invoke(reason)
                scheduleReconnect()
            } finally {
                forcingReconnect.set(false)
            }
        }
    }

    /**
     * Send a JSON text message.
     */
    private fun sendJson(json: JSONObject) {
        val ws = webSocket
        if (ws != null) {
            ws.send(json.toString())
        }
    }

    /**
     * Send heartbeat.
     */
    private fun sendHeartbeat() {
        if (!isConnected) return
        val json = JSONObject().apply {
            put("type", "heartbeat")
        }
        sendJson(json)
    }

    /**
     * Tell the server to finalize the current session (on FINISH).
     * WebSocket stays open; a new hello starts the next session.
     */
    fun endMeditationSession() {
        if (!isConnected) {
            sessionId = ""
            seqNum.set(0)
            return
        }
        val json = JSONObject().apply { put("type", "session_end") }
        sendJson(json)
        sessionId = ""
        seqNum.set(0)
        Log.i(TAG, "Sent session_end")
    }

    /**
     * Request a new server session (on GO). Resets sequence numbers.
     */
    fun startMeditationSession() {
        seqNum.set(0)
        sessionId = ""
        if (!isConnected) {
            Log.w(TAG, "Cannot start session — not connected")
            return
        }
        val hello = JSONObject().apply {
            put("type", "hello")
            put("device", deviceName)
            put("preset", preset)
        }
        sendJson(hello)
        Log.i(TAG, "Sent hello for new meditation session")
    }

    private fun sendHello() = startMeditationSession()

    // kept for clarity — initial connect defers hello until meditation starts

    // ── OkHttp WebSocket Listener ───────────────────────────────

    private val wsListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.i(TAG, "WebSocket opened: ${response.message}")
            isConnected = true
            reconnectAttempt = 0
            sessionId = ""

            // Session is created on GO via startMeditationSession(), not on connect
            onConnected?.invoke()
            startHeartbeat()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val msg = JSONObject(text)
                val type = msg.optString("type", "")

                when (type) {
                    "hello_ack" -> {
                        sessionId = msg.getString("session_id")
                        Log.i(TAG, "Session ready: $sessionId")
                        // Drain any buffered packets now that we have a session
                        scope.launch { drainBuffer() }
                        onSessionReady?.invoke(sessionId)
                    }
                    "session_ended" -> {
                        Log.i(TAG, "Server confirmed session end: ${msg.optString("session_id")}")
                    }
                    "pong" -> {
                        // Heartbeat acknowledged
                    }
                    "command" -> {
                        val cmd = msg.optString("cmd", "")
                        Log.i(TAG, "Server command: $cmd")
                        onCommand?.invoke(cmd)
                    }
                    else -> {
                        Log.d(TAG, "Server message: $type")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to parse server message: ${e.message}")
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            Log.d(TAG, "Received binary message: ${bytes.size} bytes")
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "WebSocket closing: $code $reason")
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "WebSocket closed: $code $reason")
            isConnected = false
            sessionId = ""
            heartbeatJob?.cancel()
            onDisconnected?.invoke(reason)

            // Auto-reconnect
            if (started.get()) {
                scheduleReconnect()
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.e(TAG, "WebSocket failure: ${t.message}", t)
            isConnected = false
            sessionId = ""
            heartbeatJob?.cancel()
            onDisconnected?.invoke(t.message ?: "Unknown error")

            // Auto-reconnect
            if (started.get()) {
                scheduleReconnect()
            }
        }
    }

    // ── Heartbeat ─────────────────────────────────────────────

    private fun startHeartbeat() {
        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (isActive && isConnected) {
                delay(HEARTBEAT_INTERVAL_MS)
                if (isConnected) {
                    sendHeartbeat()
                }
            }
        }
    }

    // ── Reconnection ──────────────────────────────────────────

    private fun scheduleReconnect() {
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            reconnectAttempt++
            val delayMs = minOf(
                RECONNECT_BASE_DELAY_MS * (1L shl minOf(reconnectAttempt - 1, 5)),
                RECONNECT_MAX_DELAY_MS
            )
            Log.i(TAG, "Reconnect attempt $reconnectAttempt in ${delayMs}ms")
            delay(delayMs)

            if (started.get()) {
                doConnect()
            }
        }
    }

    private fun delayStart(delayMs: Long) {
        scope.launch {
            delay(delayMs)
            if (started.get()) {
                doConnect()
            }
        }
    }
}
