package com.musebridge.gatt

import android.annotation.SuppressLint
import android.bluetooth.*
import android.content.Context
import android.os.Build
import android.util.Log
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import java.util.UUID

/**
 * Muse S Athena (Gen 3, MS_03) BLE Protocol — based on amused-py.
 *
 * Key differences from Muse 2:
 *   - All sensor data multiplexed on a SINGLE characteristic (273e0013)
 *   - TAG-based subpacket structure
 *   - 14-bit LSB-first EEG packing
 *   - dc001 must be sent TWICE: first with p21, then after switching to p1034
 *   - Commands are length-prefixed: [len+1][cmd_bytes][\\n]
 */
object MuseGatt {
    val MUSE_SERVICE = UUID.fromString("0000fe8d-0000-1000-8000-00805f9b34fb")
    val CONTROL_CHAR = UUID.fromString("273e0001-4c4d-454d-96be-f03bac821358")
    val SENSOR_CHAR  = UUID.fromString("273e0013-4c4d-454d-96be-f03bac821358")
    val CCCD         = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
}

data class MusePacket(
    val uuidSuffix: String,
    val data: ByteArray
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is MusePacket) return false
        return uuidSuffix == other.uuidSuffix && data.contentEquals(other.data)
    }
    override fun hashCode(): Int = uuidSuffix.hashCode() * 31 + data.contentHashCode()
}

enum class ConnectionState {
    DISCONNECTED, SCANNING, CONNECTING, CONNECTED, SUBSCRIBING, STREAMING, DISCONNECTING
}

class MuseGattManager(
    private val context: Context,
    private val log: ((String) -> Unit)? = null,
    private val maxReconnectAttempts: Int = 1000, // Effectively indefinite
    private val reconnectBaseDelayMs: Long = 2000L
) {
    private fun l(msg: String) { log?.invoke(msg); Log.d("MuseGatt", msg) }

    private var gatt: BluetoothGatt? = null
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var initJob: Job? = null
    private var keepAliveJob: Job? = null
    private var reconnectJob: Job? = null
    private var reconnectCount: Int = 0
    private var isIntentionalDisconnect: Boolean = false
    private var lastDevice: BluetoothDevice? = null

    private val _connectionState = MutableStateFlow(ConnectionState.DISCONNECTED)
    val connectionState: StateFlow<ConnectionState> = _connectionState

    private val _dataFlow = MutableSharedFlow<MusePacket>(replay = 0, extraBufferCapacity = 128)
    val dataFlow: SharedFlow<MusePacket> = _dataFlow

    /* Encode command per Athena protocol: [len+1][text][\\n] */
    private fun encodeCmd(text: String): ByteArray {
        val raw = text.toByteArray(Charsets.UTF_8) + byteArrayOf('\n'.code.toByte())
        return byteArrayOf((raw.size + 1).toByte()) + raw
    }

    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice) {
        l("Connecting to ${device.address}...")
        lastDevice = device
        reconnectCount = 0
        isIntentionalDisconnect = false
        _connectionState.value = ConnectionState.CONNECTING
        gatt = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        } else {
            @Suppress("DEPRECATION") device.connectGatt(context, false, gattCallback)
        }
    }

    @SuppressLint("MissingPermission")
    fun disconnect() {
        l("Disconnecting...")
        isIntentionalDisconnect = true
        reconnectJob?.cancel(); reconnectJob = null
        keepAliveJob?.cancel(); keepAliveJob = null
        _connectionState.value = ConnectionState.DISCONNECTING
        initJob?.cancel()
        scope.launch {
            try { writeCtrl(encodeCmd("h")) } catch (_: Exception) {}
            delay(300)
            gatt?.disconnect(); gatt?.close(); gatt = null
            _connectionState.value = ConnectionState.DISCONNECTED
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    l("BLE connected, requesting MTU 512...")
                    reconnectCount = 0 // Reset on successful connection
                    _connectionState.value = ConnectionState.CONNECTED
                    gatt.requestMtu(512)
                } else { 
                    l("Connect FAILED: $status")
                    _connectionState.value = ConnectionState.DISCONNECTED
                    if (!isIntentionalDisconnect) startReconnect()
                }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                // Log the status code to understand WHY
                val reason = when (status) {
                    BluetoothGatt.GATT_SUCCESS -> "local-request (normal)"
                    0x08 -> "link-timeout (out of range)"
                    0x13 -> "remote-terminated (headband off)"
                    0x16 -> "local-terminated (system kill)"
                    else -> "status=0x${status.toString(16)}"
                }
                l("BLE disconnected: $reason (reconnectCount=$reconnectCount, intentional=$isIntentionalDisconnect)")
                
                // Always close the current GATT handle to avoid "too many open connections"
                try { gatt.close() } catch (_: Exception) {}
                this@MuseGattManager.gatt = null
                
                _connectionState.value = ConnectionState.DISCONNECTED

                // Auto-reconnect if not intentionally stopped by user
                if (!isIntentionalDisconnect) {
                    startReconnect()
                }
            }
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            l("MTU: $mtu, discovering services...")
            gatt.discoverServices()
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) runAthenaInit(gatt)
            else l("Service discovery FAILED: $status")
        }

        override fun onCharacteristicChanged(gatt: BluetoothGatt, ch: BluetoothGattCharacteristic, value: ByteArray) {
            handleNotification(ch.uuid, value)
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(gatt: BluetoothGatt, ch: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION") ch.value?.let { handleNotification(ch.uuid, it) }
        }

        private fun handleNotification(uuid: UUID, value: ByteArray) {
            val s = uuid.toString().lowercase()
            val suffix = if (s.length >= 8) s.substring(4, 8) else "????"
            scope.launch {
                if (suffix == "0001") {
                    val text = String(value, Charsets.UTF_8).trim()
                    if (text.isNotEmpty()) l("Ctrl: $text")
                }
                _dataFlow.emit(MusePacket(suffix, value))
            }
        }

        override fun onCharacteristicWrite(gatt: BluetoothGatt, ch: BluetoothGattCharacteristic, status: Int) {}

        override fun onReadRemoteRssi(gatt: BluetoothGatt, rssi: Int, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS) {
                l("KeepAlive: RSSI=$rssi dBm")
            }
        }
    }

    /**
     * Athena init sequence (from amused-py):
     *   v6 → s → h → p21 → s → [enable sensor notify 273e0013] → dc001+L1 → h → p1034 → s → dc001+L1
     */
    @SuppressLint("MissingPermission")
    private fun runAthenaInit(gatt: BluetoothGatt) {
        initJob = scope.launch {
            val g = gatt
            try {
                _connectionState.value = ConnectionState.SUBSCRIBING

                // Find service and characteristics
                val svc = g.services.find { it.uuid == MuseGatt.MUSE_SERVICE }
                    ?: run { l("ERROR: Muse service not found!"); return@launch }
                val ctrlChar = svc.characteristics.find { it.uuid == MuseGatt.CONTROL_CHAR }
                    ?: run { l("ERROR: Control char 273e0001 not found!"); return@launch }
                val sensorChar = svc.characteristics.find { it.uuid == MuseGatt.SENSOR_CHAR }
                    ?: run { l("ERROR: Sensor char 273e0013 not found!"); return@launch }

                // MUST enable control notifications FIRST (before sending any commands)
                l("Init: enable control notify 273e0001")
                g.setCharacteristicNotification(ctrlChar, true)
                ctrlChar.getDescriptor(MuseGatt.CCCD)?.let {
                    it.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    g.writeDescriptor(it)
                }
                delay(100)

                l("Init: v6");  writeCtrl(encodeCmd("v6"));  delay(80)
                l("Init: s");   writeCtrl(encodeCmd("s"));   delay(80)
                l("Init: h");   writeCtrl(encodeCmd("h"));   delay(80)

                l("Init: p21"); writeCtrl(encodeCmd("p21")); delay(80)
                writeCtrl(encodeCmd("s")); delay(80)

                // Enable sensor notifications after p21 is set
                l("Init: enable sensor notify 273e0013")
                g.setCharacteristicNotification(sensorChar, true)
                sensorChar.getDescriptor(MuseGatt.CCCD)?.let {
                    it.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    g.writeDescriptor(it)
                }
                delay(100)
                l("Init: sensor notify enabled")

                l("Init: dc001+L1 (prime)"); writeCtrl(encodeCmd("dc001")); delay(80)
                writeCtrl(encodeCmd("L1")); delay(80)

                l("Init: h"); writeCtrl(encodeCmd("h")); delay(80)
                l("Init: p1034"); writeCtrl(encodeCmd("p1034")); delay(80)
                writeCtrl(encodeCmd("s")); delay(80)

                l("Init: dc001+L1 (start)"); writeCtrl(encodeCmd("dc001")); delay(80)
                writeCtrl(encodeCmd("L1")); delay(80)

                l("Init COMPLETE — waiting for data")
                _connectionState.value = ConnectionState.STREAMING
                startKeepAlive()
            } catch (e: Exception) {
                l("Init ERROR: ${e.message}")
            }
        }
    }

    /**
     * Keep-alive: periodically read RSSI to prevent BLE idle timeout.
     * Some phones drop BLE connections that have no activity for ~30 seconds.
     */
    @SuppressLint("MissingPermission")
    private fun startKeepAlive() {
        keepAliveJob?.cancel()
        keepAliveJob = scope.launch {
            while (isActive) {
                delay(30_000L)  // every 30 seconds
                try {
                    val g = gatt
                    if (g != null && _connectionState.value == ConnectionState.STREAMING) {
                        g.readRemoteRssi()
                    }
                } catch (e: Exception) {
                    l("KeepAlive error: ${e.message}")
                }
            }
        }
    }

    /**
     * Auto-reconnect with exponential backoff on unexpected disconnect.
     */
    private fun startReconnect() {
        if (isIntentionalDisconnect) return
        
        if (reconnectCount >= maxReconnectAttempts) {
            l("Reconnect exhausted ($maxReconnectAttempts attempts)")
            stopReconnect()
            return
        }
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            val device = lastDevice
            if (device == null) {
                l("Reconnect FAILED: no device reference")
                return@launch
            }
            reconnectCount++
            // Cap delay at 10 seconds for faster recovery when back in range
            val delayMs = (reconnectBaseDelayMs * (1L shl (reconnectCount - 1)))
                .coerceAtMost(10_000L) 
            
            l("Reconnect attempt $reconnectCount in ${delayMs/1000}s...")
            delay(delayMs)

            if (isIntentionalDisconnect) return@launch

            // Clean up old gatt if any
            try { gatt?.close() } catch (_: Exception) {}
            gatt = null

            l("Retrying connection to ${device.address} (autoConnect=true)...")
            _connectionState.value = ConnectionState.CONNECTING
            gatt = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                device.connectGatt(context, true, gattCallback, BluetoothDevice.TRANSPORT_LE)
            } else {
                @Suppress("DEPRECATION") device.connectGatt(context, true, gattCallback)
            }
        }
    }

    private fun stopReconnect() {
        reconnectJob?.cancel(); reconnectJob = null
        keepAliveJob?.cancel(); keepAliveJob = null
    }

    @SuppressLint("MissingPermission")
    private fun writeCtrl(bytes: ByteArray) {
        val g = gatt ?: return
        val svc = g.services.find { it.uuid == MuseGatt.MUSE_SERVICE } ?: return
        val ch = svc.characteristics.find { it.uuid == MuseGatt.CONTROL_CHAR } ?: return
        ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        ch.value = bytes
        g.writeCharacteristic(ch)
    }
}
