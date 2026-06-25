package com.musebridge.viewmodel

import android.app.Application
import android.bluetooth.BluetoothDevice
import android.content.Context
import android.net.wifi.WifiManager
import android.os.PowerManager
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.musebridge.MuseApp
import com.musebridge.gatt.ConnectionState
import com.musebridge.gatt.MuseGattManager
import com.musebridge.gatt.MuseGatt
import com.musebridge.osc.OscSender
import com.musebridge.cloud.CloudWebSocketManager
import com.musebridge.parser.DataDecoder
import com.musebridge.scanner.BleDevice
import com.musebridge.scanner.BleScanner
import com.musebridge.storage.OfflineStorageManager
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import java.io.FileInputStream

/**
 * Signal quality per EEG channel (0 = no contact, 1-4 = quality level).
 * Muse S reports contact via telemetry or signal variance.
 */
data class SignalQuality(
    val tp9: Float = 0f,   // 0..1
    val af7: Float = 0f,
    val af8: Float = 0f,
    val tp10: Float = 0f
)

data class UiState(
    val devices: List<BleDevice> = emptyList(),
    val isScanning: Boolean = false,
    val connectionState: ConnectionState = ConnectionState.DISCONNECTED,
    val connectedDeviceName: String = "",
    val signalQuality: SignalQuality = SignalQuality(),
    val averagedSignals: SignalQuality = SignalQuality(),
    val batteryPercent: Float = 0f,
    val packetCount: Long = 0,
    val cloudUrl: String = "ws://118.24.80.184:8000/ws/session",
    val cloudConnected: Boolean = false,
    val cloudSessionId: String = "",
    val logLines: List<String> = emptyList(),
    val isMeditating: Boolean = false,
    val meditationDurationSeconds: Long = 0
)

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val museApp = app as MuseApp
    val scanner = BleScanner(museApp)
    val gattManager = MuseGattManager(app, log = { appendLog(it) })
    val oscSender = OscSender(viewModelScope)
    val cloudWs = CloudWebSocketManager(viewModelScope)
    private val storage = OfflineStorageManager(app)

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState.asStateFlow()

    // 3-second smoothing window
    private val smoothingWindow = mutableListOf<SignalQuality>()
    private var lastSmoothingTime = 0L

    // Accumulate per-channel EEG data for signal quality calculation
    private val eegAccumulators = Array(5) { mutableListOf<Float>() }

    // Keep device awake during streaming / meditation
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var releaseWakelocksJob: kotlinx.coroutines.Job? = null
    private var sessionHadCloudIssues = false

    init {
        // Observe scanner devices
        viewModelScope.launch {
            scanner.devices.collect { devices ->
                _uiState.update { it.copy(devices = devices) }
            }
        }
        viewModelScope.launch {
            scanner.isScanning.collect { scanning ->
                _uiState.update { state ->
                    val nextConnState = if (scanning) {
                        ConnectionState.SCANNING
                    } else if (state.connectionState == ConnectionState.SCANNING) {
                        ConnectionState.DISCONNECTED
                    } else {
                        state.connectionState
                    }
                    state.copy(isScanning = scanning, connectionState = nextConnState)
                }
            }
        }

        // Observe connection state
        viewModelScope.launch {
            gattManager.connectionState.collect { state ->
                _uiState.update { it.copy(connectionState = state) }
                appendLog("State: $state")

                if (state == ConnectionState.DISCONNECTED) {
                    // Reset signal quality when disconnected
                    _uiState.update { it.copy(signalQuality = SignalQuality(), averagedSignals = SignalQuality()) }
                    eegAccumulators.forEach { it.clear() }
                }

                // Manage wake locks based on connection state
                when (state) {
                    ConnectionState.STREAMING, ConnectionState.SUBSCRIBING,
                    ConnectionState.CONNECTING, ConnectionState.CONNECTED -> {
                        acquireWakeLocks()
                        releaseWakelocksJob?.cancel(); releaseWakelocksJob = null
                    }
                    ConnectionState.DISCONNECTED -> {
                        if (_uiState.value.isMeditating) {
                            // Keep CPU/network alive during meditation reconnect
                            acquireWakeLocks()
                            releaseWakelocksJob?.cancel(); releaseWakelocksJob = null
                            appendLog("WakeLock: held during meditation (BLE reconnecting)")
                        } else {
                            scheduleWakelockRelease(120_000L)
                        }
                    }
                    ConnectionState.DISCONNECTING -> {
                        if (!_uiState.value.isMeditating) {
                            releaseWakelocksJob?.cancel()
                            releaseWakeLocks()
                        }
                    }
                    else -> { /* SCANNING — keep current lock state */ }
                }
            }
        }

        // Observe BLE data and forward to OSC + Cloud
        viewModelScope.launch {
            gattManager.dataFlow.collect { packet ->
                handleDataPacket(packet)
            }
        }

        // Cloud WebSocket callbacks
        cloudWs.onConnected = {
            _uiState.update { it.copy(cloudConnected = true) }
            appendLog("Cloud: connected")
            // Try uploading background data if not meditating
            checkAndUploadOfflineData()
        }
        cloudWs.onDisconnected = { reason ->
            _uiState.update { it.copy(cloudConnected = false, cloudSessionId = "") }
            appendLog("Cloud: disconnected ($reason)")
            sessionHadCloudIssues = true

            if (_uiState.value.isMeditating) {
                appendLog("Cloud lost during Zen — local backup continues")
            }
        }
        cloudWs.onSessionReady = { sid ->
            _uiState.update { it.copy(cloudSessionId = sid) }
            appendLog("Cloud: session $sid")
        }
        cloudWs.onCommand = { cmd ->
            appendLog("Cloud: cmd=$cmd")
            when (cmd) {
                "halt" -> gattManager.disconnect()
            }
        }

        // AUTO-CHECK: Connect to cloud server immediately to check online status
        val url = _uiState.value.cloudUrl
        if (url.isNotBlank()) {
            cloudWs.connect(url, "Zen-User", "p1034")
        }
    }

    fun startScan() {
        scanner.startScan()
    }

    fun stopScan() {
        scanner.stopScan()
    }

    fun connectToDevice(device: BleDevice) {
        stopScan()
        _uiState.update { it.copy(connectedDeviceName = device.name ?: device.address) }

        // Get BluetoothDevice from address
        val btDevice = try {
            val adapter = android.bluetooth.BluetoothManager::class.java
                .let { museApp.getSystemService(it) }?.adapter
            adapter?.getRemoteDevice(device.address)
        } catch (e: Exception) { null }

        if (btDevice != null) {
            gattManager.connect(btDevice)
        }
    }

    fun disconnect() {
        gattManager.disconnect()
        oscSender.stop()
        cloudWs.disconnect()
        releaseWakeLocks()
        _uiState.update { it.copy(signalQuality = SignalQuality(), batteryPercent = 0f,
            cloudConnected = false, cloudSessionId = "") }
    }

    private fun acquireWakeLocks() {
        if (wakeLock == null) {
            val pm = getApplication<MuseApp>().getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "MuseCloud:Streaming"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
            appendLog("WakeLock: acquired (CPU stays on)")
        }
        if (wifiLock == null) {
            val wm = getApplication<MuseApp>().getSystemService(Context.WIFI_SERVICE) as WifiManager
            // Use HIGH_PERF mode to minimize latency and packet loss when screen is off.
            // This is critical for maintaining stable WebSocket connections in Doze mode.
            wifiLock = wm.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                "MuseCloud:WifiLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
            appendLog("WifiLock: acquired (High Perf mode)")
        }
    }

    /**
     * Schedule a delayed wakelock release to allow time for auto-reconnect.
     * If the device reconnects before the delay expires, the release is cancelled.
     */
    private fun scheduleWakelockRelease(delayMs: Long) {
        releaseWakelocksJob?.cancel()
        releaseWakelocksJob = viewModelScope.launch {
            appendLog("WakeLock: will release in ${delayMs/1000}s (if no reconnect)")
            delay(delayMs)
            appendLog("WakeLock: grace period expired, releasing")
            releaseWakeLocks()
        }
    }

    private fun releaseWakeLocks() {
        wakeLock?.let {
            if (it.isHeld) { it.release(); appendLog("WakeLock: released") }
            wakeLock = null
        }
        wifiLock?.let {
            if (it.isHeld) { it.release(); appendLog("WifiLock: released") }
            wifiLock = null
        }
    }

    fun updateCloudUrl(url: String) {
        _uiState.update { it.copy(cloudUrl = url) }
    }

    // Local mode target (configurable IP:port)
    private var localHost = "192.168.2.5"
    private var localPort = 5000

    fun getLocalTarget(): String = "$localHost:$localPort"

    fun updateLocalTarget(host: String, port: Int) {
        localHost = host
        localPort = port
        oscSender.configure(host, port)
        if (oscSender.isRunning) {
            oscSender.stop()
            oscSender.start()
        }
        appendLog("Local target: $host:$port")
    }

    fun updateOscTarget(host: String, port: Int) {
        oscSender.configure(host, port)
    }

    /**
     * Toggle local mode: when enabled, start OSC sender targeting the desktop.
     * When disabled, stop OSC sender (cloud WebSocket handles transmission).
     */
    fun setLocalMode(enabled: Boolean) {
        if (enabled) {
            updateOscTarget(localHost, localPort)
            oscSender.start()
            appendLog("Local Mode: OSC → $localHost:$localPort")
        } else {
            oscSender.stop()
            appendLog("Local Mode: disabled")
        }
    }

    fun setMeditation(active: Boolean) {
        _uiState.update { it.copy(isMeditating = active, meditationDurationSeconds = 0) }
        appendLog("Meditation: ${if (active) "Started" else "Stopped"}")

        if (active) {
            sessionHadCloudIssues = false
            acquireWakeLocks()
            startTimer()
            storage.startSession()

            if (_uiState.value.cloudConnected) {
                cloudWs.startMeditationSession()
                appendLog("Cloud: requesting new session...")
            } else {
                appendLog("Server is offline. Saving locally...")
            }
        } else {
            stopTimer()
            storage.endSession()
            appendLog("Zen session ended")

            if (_uiState.value.cloudConnected) {
                cloudWs.endMeditationSession()
                _uiState.update { it.copy(cloudSessionId = "") }
                appendLog("Cloud: session ended")
            }

            if (!sessionHadCloudIssues && _uiState.value.cloudConnected && cloudWs.dropCount == 0L) {
                val files = storage.getPendingFiles()
                if (files.isNotEmpty()) {
                    storage.deleteFile(files.last())
                    appendLog("Cloud OK — removed redundant local backup")
                }
            } else {
                appendLog("Local backup retained (cloud drops=${cloudWs.dropCount})")
            }

            releaseWakeLocks()
            checkAndUploadOfflineData()
        }
    }

    private var uploadJob: Job? = null
    private fun checkAndUploadOfflineData() {
        if (uploadJob?.isActive == true) return
        if (!_uiState.value.cloudConnected || _uiState.value.isMeditating) return

        uploadJob = viewModelScope.launch(Dispatchers.IO) {
            val pendingFiles = storage.getPendingFiles()
            if (pendingFiles.isEmpty()) return@launch

            appendLog("Found ${pendingFiles.size} offline sessions. Starting upload...")

            for (file in pendingFiles) {
                if (!isActive || !uiState.value.cloudConnected || uiState.value.isMeditating) break

                cloudWs.startMeditationSession()
                // Wait for server to assign a session_id for this upload batch
                var waited = 0
                while (isActive && cloudWs.sessionId.isEmpty() && waited < 50) {
                    delay(100)
                    waited++
                }
                if (cloudWs.sessionId.isEmpty()) {
                    appendLog("Upload aborted — no cloud session for ${file.name}")
                    break
                }

                appendLog("Uploading ${file.name} to session ${cloudWs.sessionId}...")
                FileInputStream(file).use { fis ->
                    var packet = storage.readNextPacket(fis)
                    while (packet != null && isActive && uiState.value.cloudConnected && !uiState.value.isMeditating) {
                        cloudWs.sendPacket(packet)
                        packet = storage.readNextPacket(fis)
                        // Throttle slightly to not overwhelm the network/server for historical data
                        delay(10) 
                    }
                }
                
                if (isActive && !uiState.value.isMeditating) {
                    storage.deleteFile(file)
                    appendLog("Uploaded and deleted ${file.name}")
                    cloudWs.endMeditationSession()
                }
            }
            appendLog("Background upload complete.")
        }
    }

    private var timerJob: Job? = null
    private fun startTimer() {
        timerJob?.cancel()
        val startTime = System.currentTimeMillis()
        timerJob = viewModelScope.launch {
            while (isActive) {
                val elapsed = (System.currentTimeMillis() - startTime) / 1000
                _uiState.update { it.copy(meditationDurationSeconds = elapsed) }
                delay(1000)
            }
        }
    }

    private fun stopTimer() {
        timerJob?.cancel()
        timerJob = null
    }

    /**
     * Handle a raw BLE data packet: decode → accumulate → send OSC.
     */
    /** Track first-seen suffixes to avoid log spam */
    private var loggedSuffixes = mutableSetOf<String>()

    private val maxLogLines = 200

    private fun appendLog(msg: String) {
        val lines = _uiState.value.logLines.toMutableList()
        val ts = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
            .format(java.util.Date())
        lines.add("$ts $msg")
        if (lines.size > maxLogLines) {
            lines.removeAt(0)
        }
        _uiState.update { it.copy(logLines = lines) }
    }

    private var packetCounter = 0

    private fun handleDataPacket(packet: com.musebridge.gatt.MusePacket) {
        val suffix = packet.uuidSuffix
        val data = packet.data
        packetCounter++

        if (suffix == "0013") {
            // Athena multiplexed sensor data — parse TAG-based subpackets
            val subpackets = DataDecoder.parsePayload(data)

            if (loggedSuffixes.add(suffix)) {
                appendLog("SENSOR: ${data.size}B, ${subpackets.size} subpkts")
            }

            // ── Data Export: always local backup during meditation + cloud when available ──
            if (uiState.value.isMeditating) {
                storage.savePacket(data)
                if (uiState.value.cloudConnected && cloudWs.sessionId.isNotEmpty()) {
                    if (!cloudWs.sendPacket(data)) {
                        sessionHadCloudIssues = true
                    }
                } else {
                    sessionHadCloudIssues = true
                }
            }

            for (sp in subpackets) {
                if (sp.hasEeg && sp.eeg != null) {
                    // EEG: (nSamples × nChannels) flat, row-major
                    // Batch all samples into one OSC message: ns*ch floats
                    val ch = sp.eegChannels
                    val ns = sp.eegSamples
                    val batch = FloatArray(ns * 5)
                    for (s in 0 until ns) {
                        for (c in 0 until minOf(ch, 5)) {
                            batch[s * 5 + c] = sp.eeg[s * ch + c]
                        }
                    }
                    oscSender.sendFloats("/muse/eeg", batch)

                    // Update signal quality from last sample in batch
                    for (c in 0 until minOf(ch, 4)) {
                        accumulateEeg(c, floatArrayOf(batch[(ns-1) * 5 + c]))
                    }
                    updateSignalQuality()
                }
                if (sp.hasAccGyro) {
                    sp.accel?.let { oscSender.sendFloats("/muse/acc", it) }
                    sp.gyro?.let { oscSender.sendFloats("/muse/gyro", it) }
                }
                if (sp.hasPpg && sp.ppg != null) {
                    // Send all PPG samples in one batch: ns*ch floats
                    oscSender.sendFloats("/muse/ppg", sp.ppg)
                }
            }
        }

        if (packetCounter % 50 == 0) {
            appendLog("pkts: $packetCounter, cloud: ${cloudWs.packetCount} pkt, ${cloudWs.dropCount} drop")
        }
    }

    private fun accumulateEeg(channel: Int, samples: FloatArray) {
        eegAccumulators[channel].addAll(samples.toList())
        // Keep only last 64 samples for variance calculation
        if (eegAccumulators[channel].size > 64) {
            eegAccumulators[channel] = eegAccumulators[channel]
                .subList(eegAccumulators[channel].size - 64, eegAccumulators[channel].size)
                .toMutableList()
        }
    }

    /**
     * Estimate signal quality from EEG sample variance.
     * Higher variance = better contact (actual EEG signal has amplitude).
     * Near-zero variance = poor contact (flatline).
     */
    private fun updateSignalQuality() {
        val quality = FloatArray(4)
        for (ch in 0 until 4) {
            val values = eegAccumulators[ch]
            if (values.size < 12) {
                quality[ch] = 0f
                continue
            }
            val mean = values.sum() / values.size
            val variance = values.map { (it - mean) * (it - mean) }.sum() / values.size
            val stdDev = kotlin.math.sqrt(variance)

            // Map stdDev to 0..1: ~0μV std = 0, >70μV std = 1
            // Increased from 50uV to 70uV as requested to be more stable.
            quality[ch] = (stdDev / 70f).coerceIn(0f, 1f)
        }

        val currentQuality = SignalQuality(
            tp9 = quality[0],
            af7 = quality[1],
            af8 = quality[2],
            tp10 = quality[3]
        )

        _uiState.update { it.copy(signalQuality = currentQuality) }

        // Smoothing logic: update averagedSignals every 3000ms
        smoothingWindow.add(currentQuality)
        val now = System.currentTimeMillis()
        if (now - lastSmoothingTime >= 3000L) {
            val avgTp9 = smoothingWindow.map { it.tp9 }.average().toFloat()
            val avgAf7 = smoothingWindow.map { it.af7 }.average().toFloat()
            val avgAf8 = smoothingWindow.map { it.af8 }.average().toFloat()
            val avgTp10 = smoothingWindow.map { it.tp10 }.average().toFloat()

            _uiState.update { 
                it.copy(averagedSignals = SignalQuality(avgTp9, avgAf7, avgAf8, avgTp10)) 
            }
            smoothingWindow.clear()
            lastSmoothingTime = now
        }
    }

    override fun onCleared() {
        super.onCleared()
        releaseWakeLocks()
        gattManager.disconnect()
        oscSender.stop()
        cloudWs.disconnect()
        scanner.stopScan()
    }
}
