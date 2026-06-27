package com.musebridge.osc

import android.content.Context
import android.util.Log
import com.musebridge.net.UdpNetworkBinding
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicLong

/**
 * Forwards raw Muse BLE payloads to the local server over UDP.
 *
 * Frame: 0x4D 0x01 | seq u32 | length u16 | payload bytes
 */
class RawUdpSender(
    private val scope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
) {
    companion object {
        private const val MAGIC_0 = 0x4D.toByte()
        private const val MAGIC_1 = 0x01.toByte()
    }

    private var socket: DatagramSocket? = null
    private var address: InetAddress? = null
    private var port: Int = 5000
    @Volatile private var running = false

    private var sendJob: Job? = null
    private var sendChannel: Channel<ByteArray>? = null
    private val seqNum = AtomicLong(0)

    @Volatile var packetCount: Long = 0
        private set
    @Volatile var dropCount: Long = 0
        private set

    val isRunning: Boolean get() = running

    fun configure(host: String, port: Int) {
        this.address = InetAddress.getByName(host)
        this.port = port
    }

    fun start(context: Context) {
        if (running) return
        if (address == null) {
            Log.e("RawUdpSender", "start() called before configure()")
            return
        }
        running = true
        packetCount = 0
        dropCount = 0
        seqNum.set(0)

        socket = DatagramSocket()
        UdpNetworkBinding.bindToWifi(context, socket!!)
        val ch = Channel<ByteArray>(512)
        sendChannel = ch

        sendJob = scope.launch {
            for (payload in ch) {
                try {
                    val addr = address ?: continue
                    val frame = buildFrame(payload, seqNum.getAndIncrement())
                    val packet = DatagramPacket(frame, frame.size, addr, port)
                    socket?.send(packet)
                    packetCount++
                } catch (e: Exception) {
                    dropCount++
                    Log.e("RawUdpSender", "Send failed: ${e.message}")
                    delay(50)
                }
            }
        }
    }

    fun stop() {
        running = false
        sendChannel?.close()
        sendJob?.cancel()
        try { socket?.close() } catch (_: Exception) {}
        socket = null
        sendChannel = null
        sendJob = null
    }

    fun send(payload: ByteArray) {
        if (!running) return
        val ch = sendChannel ?: return
        if (!ch.trySend(payload).isSuccess) dropCount++
    }

    private fun buildFrame(payload: ByteArray, seq: Long): ByteArray {
        val buf = ByteBuffer.allocate(8 + payload.size)
        buf.put(MAGIC_0)
        buf.put(MAGIC_1)
        buf.putInt(seq.toInt())
        buf.putShort(payload.size.toShort())
        buf.put(payload)
        return buf.array()
    }
}
