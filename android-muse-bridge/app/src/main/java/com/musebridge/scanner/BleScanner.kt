package com.musebridge.scanner

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.location.LocationManager
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import android.widget.Toast
import com.musebridge.MuseApp
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * BLE result sealed class wrapping [BluetoothDevice].
 */
data class BleDevice(
    val address: String,
    val name: String?,
    val rssi: Int
)

/**
 * BLE scanner with active scanning, filtering for Muse devices by service UUID,
 * name prefix, and continuous scan callback delivery.
 */
class BleScanner(private val app: MuseApp) {

    companion object {
        /** Muse vendor 16-bit service UUID (same as 0xFE8D in Python ble_adv_visualizer.py). */
        val MUSE_SERVICE_UUID: ParcelUuid = ParcelUuid.fromString("0000FE8D-0000-1000-8000-00805F9B34FB")

        /** Muse name prefixes for post-filter. */
        val MUSE_NAME_PREFIXES = listOf("Muse", "MuseS", "Muse-")

        /** Scan timeout for a single scan session. */
        const val SCAN_TIMEOUT_MS = 15_000L
    }

    private val bluetoothAdapter: BluetoothAdapter? by lazy {
        android.bluetooth.BluetoothManager::class.java.let { cls ->
            app.getSystemService(cls)
        }?.adapter
    }

    private var scanner: BluetoothLeScanner? = null
    private var scanJob: Job? = null
    private var scanCallback: ScanCallback? = null

    /** Live list of discovered Muse devices (deduplicated by address). */
    private val _devices = MutableStateFlow<List<BleDevice>>(emptyList())
    val devices: StateFlow<List<BleDevice>> = _devices

    /** Whether a scan is currently active. */
    private val _isScanning = MutableStateFlow(false)
    val isScanning: StateFlow<Boolean> = _isScanning

    /**
     * Start BLE scan for Muse devices.
     */
    @SuppressLint("MissingPermission")
    fun startScan() {
        // Stop any existing scan first
        stopScan()

        val adapter = bluetoothAdapter ?: return
        if (!adapter.isEnabled) {
            Log.e("BleScanner", "Bluetooth is disabled.")
            return
        }

        // Check if Location is enabled (required for BLE scanning on most devices)
        val lm = app.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val isLocationEnabled = lm.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        if (!isLocationEnabled) {
            Log.e("BleScanner", "Location services are disabled. BLE scan may fail or return no results.")
            // We'll continue anyway, but this is a likely cause of issues
        }
        
        scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            Log.e("BleScanner", "BluetoothLeScanner is null. Is Bluetooth enabled?")
            return
        }

        val seen = mutableMapOf<String, BleDevice>()

        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setReportDelay(0)
            .build()

        // Scan for everything to be sure
        val filters = emptyList<ScanFilter>()

        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val device = result.device
                val rssi = result.rssi
                val name = device.name ?: "Unknown"

                Log.d("BleScanner", "Discovered: $name [${device.address}] RSSI: $rssi")

                // Filter logic
                val serviceUuids = result.scanRecord?.serviceUuids
                val hasMuseService = serviceUuids?.contains(MUSE_SERVICE_UUID) ?: false
                val matchesName = MUSE_NAME_PREFIXES.any { name.startsWith(it, ignoreCase = true) }

                if (matchesName || hasMuseService) {
                    seen[device.address] = BleDevice(
                        address = device.address,
                        name = if (name == "Unknown" && hasMuseService) "Muse (via UUID)" else name,
                        rssi = rssi
                    )
                    _devices.value = seen.values.toList().sortedByDescending { it.rssi }
                }
            }

            override fun onScanFailed(errorCode: Int) {
                Log.e("BleScanner", "Scan failed with error code: $errorCode")
                _isScanning.value = false
                
                val msg = when(errorCode) {
                    6 -> "Scan failed (status 6): System resources exhausted. Please restart Bluetooth or wait."
                    else -> "Scan failed with error code: $errorCode"
                }
                Handler(Looper.getMainLooper()).post {
                    Toast.makeText(app, msg, Toast.LENGTH_LONG).show()
                }
            }
        }
        this.scanCallback = callback

        _isScanning.value = true
        Log.d("BleScanner", "Starting BLE scan...")
        scanner?.startScan(filters, settings, callback)

        // Auto-stop after timeout
        scanJob = CoroutineScope(Dispatchers.Main).launch {
            delay(SCAN_TIMEOUT_MS)
            if (_isScanning.value) {
                Log.d("BleScanner", "Scan timeout reached.")
                stopScan()
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun stopScan() {
        scanJob?.cancel()
        scanJob = null
        scanCallback?.let {
            Log.d("BleScanner", "Stopping BLE scan.")
            scanner?.stopScan(it)
            scanCallback = null
        }
        _isScanning.value = false
    }

    fun clear() {
        _devices.value = emptyList()
    }
}
