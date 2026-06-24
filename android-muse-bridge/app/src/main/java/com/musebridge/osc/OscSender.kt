package com.musebridge.osc

import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/**
 * Sends OSC packets over UDP with a bounded send queue.
 *
 * Supports stop/restart: each [start] creates a fresh channel and coroutine.
 */
class OscSender(
    private val scope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
) {
    private var socket: DatagramSocket? = null
    private var address: InetAddress? = null
    private var port: Int = 5000
    @Volatile private var running = false

    private var sendJob: Job? = null
    private var sendChannel: Channel<ByteArray>? = null

    /** Packet counters for diagnostics */
    @Volatile var packetCount: Long = 0
        private set
    @Volatile var dropCount: Long = 0
        private set

    val isRunning: Boolean get() = running

    /**
     * Configure the target address. Call before [start].
     */
    fun configure(host: String, port: Int) {
        this.address = InetAddress.getByName(host)
        this.port = port
    }

    /**
     * Start the UDP sender coroutine. Safe to call multiple times.
     */
    fun start() {
        if (running) return
        running = true
        packetCount = 0
        dropCount = 0

        socket = DatagramSocket()
        // Larger buffer: 256 elements to tolerate brief network pauses
        val ch = Channel<ByteArray>(256)
        sendChannel = ch
        val coroutineStartTime = System.currentTimeMillis()

        sendJob = scope.launch {
            var lastPktTime = System.currentTimeMillis()
            for (data in ch) {
                try {
                    val addr = address ?: continue
                    val packet = DatagramPacket(data, data.size, addr, port)
                    socket?.send(packet)
                    packetCount++
                    lastPktTime = System.currentTimeMillis()
                } catch (e: Exception) {
                    dropCount++
                    val msg = e.message ?: e.toString()
                    Log.e("OscSender", "Send failed ($dropCount drops): $msg")
                    // Brief pause on errors to avoid tight loop
                    kotlinx.coroutines.delay(50)
                }

                // Watchdog: if send() is mysteriously slow, log a gap
                val gap = System.currentTimeMillis() - lastPktTime
                if (gap > 10_000 && packetCount > 0) {
                    Log.w("OscSender", "Send gap ${gap}ms — possible stall at pkt=$packetCount, drops=$dropCount")
                    lastPktTime = System.currentTimeMillis() // reset to avoid spam
                }
            }
            Log.w("OscSender", "Send loop exited after ${System.currentTimeMillis() - coroutineStartTime}ms, sent=$packetCount, drops=$dropCount")
        }
    }

    private var sendFailCount: Long = 0
    private var lastFailLogTime: Long = 0

    /**
     * Enqueue an OSC message for sending. Non-blocking.
     */
    fun send(data: ByteArray) {
        if (!running) {
            sendFailCount++
            if (sendFailCount == 1L || sendFailCount % 1000L == 0L) {
                Log.w("OscSender", "send() called but not running (failCount=$sendFailCount)")
            }
            return
        }
        val result = sendChannel?.trySend(data)
        if (result != null && result.isFailure) {
            dropCount++
            val now = System.currentTimeMillis()
            if (now - lastFailLogTime > 5000) {
                lastFailLogTime = now
                Log.w("OscSender", "Channel full or closed, drops=$dropCount")
            }
        }
    }

    /**
     * Convenience: send float array on given OSC path.
     */
    fun sendFloats(path: String, values: FloatArray) {
        send(OscEncoder.encodeFloats(path, values))
    }

    /**
     * Convenience: send single float.
     */
    fun sendFloat(path: String, value: Float) {
        send(OscEncoder.encodeFloat(path, value))
    }

    /**
     * Convenience: send single string.
     */
    fun sendString(path: String, value: String) {
        send(OscEncoder.encodeString(path, value))
    }

    /**
     * Stop sender and close socket.
     */
    fun stop() {
        running = false
        sendJob?.cancel()
        sendJob = null
        sendChannel?.close()
        sendChannel = null
        try { socket?.close() } catch (_: Exception) {}
        socket = null
    }
}
