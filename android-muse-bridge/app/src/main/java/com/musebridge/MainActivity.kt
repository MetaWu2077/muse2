package com.musebridge

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.View
import android.widget.Toast
import java.net.Inet4Address
import java.net.NetworkInterface
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.musebridge.databinding.ActivityMainBinding
import com.musebridge.gatt.ConnectionState
import com.musebridge.scanner.BleDevice
import com.musebridge.service.StreamForegroundService
import com.musebridge.viewmodel.MainViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var viewModel: MainViewModel
    private lateinit var deviceAdapter: DeviceAdapter
    private var autoConnectAttempted = false

    // Permission launchers
    private val blePermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        Log.d("Permissions", "Permission results: $grants")
        val allGranted = grants.values.all { it }
        if (allGranted) {
            Log.d("Permissions", "All permissions granted, starting scan")
            lifecycleScope.launch {
                delay(500) // Small delay after permission grant
                viewModel.startScan()
            }
        } else {
            val denied = grants.filter { !it.value }.keys
            Log.e("Permissions", "Permissions denied: $denied")
            Toast.makeText(this, "Permission required: $denied", Toast.LENGTH_LONG).show()
        }
    }

    private val bluetoothEnableLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { /* Bluetooth enabled, proceed */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        viewModel = ViewModelProvider(
            this,
            ViewModelProvider.AndroidViewModelFactory.getInstance(application)
        )[MainViewModel::class.java]

        setupDeviceList()
        setupButtons()
        observeState()

        // Request battery optimization and auto-start scan
        checkBatteryOptimization()
        
        if (hasBlePermissions()) {
            viewModel.startScan()
        } else {
            requestBlePermissions()
        }
    }

    /**
     * Check if the app is exempt from battery optimization.
     * If not, prompt user to grant exemption to prevent Doze from killing the stream.
     */
    private fun checkBatteryOptimization() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            MaterialAlertDialogBuilder(this)
                .setTitle("Battery Optimization")
                .setMessage(
                    "For reliable long-term streaming (30+ min), Muse Cloud needs to be " +
                    "exempt from battery optimization.\n\n" +
                    "Without this, Android may pause network data after ~20 min when " +
                    "the screen is off.\n\n" +
                    "Tap 'Allow' to exempt this app."
                )
                .setPositiveButton("Allow") { _, _ ->
                    val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                        data = Uri.parse("package:$packageName")
                    }
                    startActivity(intent)
                }
                .setNegativeButton("Later", null)
                .show()
        } else {
            Log.d("MainActivity", "Battery optimization already exempt")
        }
    }

    /**
     * Keep foreground service alive during meditation and BLE reconnect.
     * Stopping on brief DISCONNECTED was causing Doze to kill network after ~20 min.
     */
    private fun updateForegroundService(state: com.musebridge.viewmodel.UiState) {
        val needsService = state.isMeditating || state.connectionState in setOf(
            ConnectionState.STREAMING,
            ConnectionState.SUBSCRIBING,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED
        )

        if (needsService) {
            val intent = Intent(this, StreamForegroundService::class.java).apply {
                putExtra(StreamForegroundService.EXTRA_DEVICE_NAME,
                    state.connectedDeviceName.ifEmpty { "Muse S" })
                putExtra(StreamForegroundService.EXTRA_IS_MEDITATING, state.isMeditating)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
        } else if (state.connectionState == ConnectionState.DISCONNECTED && !state.isMeditating) {
            stopService(Intent(this, StreamForegroundService::class.java))
        }
    }

    private fun setupDeviceList() {
        deviceAdapter = DeviceAdapter { device -> onDeviceSelected(device) }
        binding.rvDevices.layoutManager = LinearLayoutManager(this)
        binding.rvDevices.adapter = deviceAdapter
    }

    private fun setupButtons() {
        binding.btnAction.setOnClickListener {
            val state = viewModel.uiState.value
            if (state.isMeditating) {
                viewModel.setMeditation(false)
                Toast.makeText(this, "Session Saved", Toast.LENGTH_SHORT).show()
            } else {
                // Check server status before starting
                if (!state.cloudConnected) {
                    MaterialAlertDialogBuilder(this)
                        .setTitle("Server Offline")
                        .setMessage("The cloud server is currently unreachable. Your session will be saved locally and uploaded later when the connection is restored.")
                        .setPositiveButton("Start Offline") { _, _ ->
                            viewModel.setMeditation(true)
                        }
                        .setNegativeButton("Wait", null)
                        .show()
                } else {
                    viewModel.setMeditation(true)
                }
            }
        }

        // Long press title to toggle log visibility
        binding.tvTitle.setOnLongClickListener {
            binding.svLog.visibility = if (binding.svLog.visibility == View.VISIBLE) View.GONE else View.VISIBLE
            true
        }

        // Local mode toggle — sends OSC to desktop instead of cloud
        binding.switchLocalMode.setOnCheckedChangeListener { _, isChecked ->
            viewModel.setLocalMode(isChecked)
            if (isChecked) {
                Toast.makeText(this, "Local Mode: sending to " + viewModel.getLocalTarget(),
                    Toast.LENGTH_SHORT).show()
            } else {
                Toast.makeText(this, "Cloud Mode", Toast.LENGTH_SHORT).show()
            }
        }

        // Tap the label to change local target IP
        binding.switchLocalMode.setOnLongClickListener {
            showLocalTargetDialog()
            true
        }
    }

    private fun observeState() {
        lifecycleScope.launch {
            viewModel.uiState.collectLatest { state ->
                // Auto-connect once on first discovery; MuseGattManager handles reconnect internally
                if (!autoConnectAttempted &&
                    state.connectionState == ConnectionState.DISCONNECTED &&
                    state.devices.isNotEmpty()
                ) {
                    autoConnectAttempted = true
                    onDeviceSelected(state.devices.first())
                }
                updateUi(state)
            }
        }
    }

    private fun updateUi(state: com.musebridge.viewmodel.UiState) {
        updateForegroundService(state)

        // Signal Readiness logic: Use 3-second averaged signals for stability
        val tp9Ready = isSignalReady(state.averagedSignals.tp9)
        val af7Ready = isSignalReady(state.averagedSignals.af7)
        val af8Ready = isSignalReady(state.averagedSignals.af8)
        val tp10Ready = isSignalReady(state.averagedSignals.tp10)
        val allSignalsReady = tp9Ready && af7Ready && af8Ready && tp10Ready && 
                             state.connectionState == ConnectionState.STREAMING

        // UI Mode Handling
        if (state.isMeditating) {
            binding.btnAction.text = "FINISH"
            binding.btnAction.isEnabled = true
            binding.btnAction.backgroundTintList = getColorStateList(R.color.zen_warning)
            
            binding.tvTimer.visibility = View.VISIBLE
            binding.vWaveform.visibility = View.VISIBLE
            
            val minutes = state.meditationDurationSeconds / 60
            val seconds = state.meditationDurationSeconds % 60
            binding.tvTimer.text = String.format("%02d:%02d", minutes, seconds)
            
            // Update waveform based on real-time average signal quality
            val avgQuality = (state.signalQuality.tp9 + state.signalQuality.af7 + 
                              state.signalQuality.af8 + state.signalQuality.tp10) / 4f
            binding.vWaveform.addSample(avgQuality)
        } else {
            binding.btnAction.text = "GO"
            binding.btnAction.isEnabled = allSignalsReady
            binding.btnAction.backgroundTintList = getColorStateList(
                if (allSignalsReady) R.color.zen_accent else R.color.zen_card
            )
            
            binding.tvTimer.visibility = View.GONE
            binding.vWaveform.visibility = View.GONE
        }

        // Always update status text regardless of meditation mode
        binding.tvStatus.text = when (state.connectionState) {
            ConnectionState.DISCONNECTED -> "Searching..."
            ConnectionState.SCANNING -> "Scanning..."
            ConnectionState.CONNECTING -> "Connecting..."
            ConnectionState.CONNECTED, ConnectionState.SUBSCRIBING -> "Initialising..."
            ConnectionState.STREAMING -> if (allSignalsReady) "Ready" else "Adjust Headband"
            ConnectionState.DISCONNECTING -> "Closing..."
        }

        // Update Cloud Status (Top Right)
        if (state.cloudConnected) {
            binding.vCloudDot.backgroundTintList = getColorStateList(R.color.zen_accent)
            binding.tvCloudStatus.text = "SERVER ONLINE"
            binding.tvCloudStatus.setTextColor(getColor(R.color.zen_accent))
        } else {
            binding.vCloudDot.backgroundTintList = getColorStateList(R.color.zen_warning)
            binding.tvCloudStatus.text = "SERVER OFFLINE"
            binding.tvCloudStatus.setTextColor(getColor(R.color.zen_text_main))
        }
        
        // Update Battery
        binding.tvBattery.text = if (state.batteryPercent > 0) "BATTERY: ${state.batteryPercent.toInt()}%" else "BATTERY: --%"

        // Update Device Name
        binding.tvDeviceName.text = state.connectedDeviceName.ifEmpty { "No Device" }

        // Update EEG Dots (Using 3s average for stability)
        updateSignalDot(binding.dotTp9, tp9Ready)
        updateSignalDot(binding.dotAf7, af7Ready)
        updateSignalDot(binding.dotAf8, af8Ready)
        updateSignalDot(binding.dotTp10, tp10Ready)

        // Sensors (PPG/ACC)
        val sensorTint = if (state.connectionState == ConnectionState.STREAMING) R.color.zen_accent else R.color.signal_off
        binding.ivSensorPpg.imageTintList = getColorStateList(sensorTint)
        binding.ivSensorAcc.imageTintList = getColorStateList(sensorTint)

        // Data log
        if (state.logLines.isNotEmpty() && state.logLines.last() != binding.tvDataLog.tag) {
            binding.tvDataLog.tag = state.logLines.last()
            binding.tvDataLog.text = state.logLines.joinToString("\n")
            binding.svLog.post { binding.svLog.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun isSignalReady(quality: Float): Boolean {
        // ViewModel maps stdDev to 0..1 (0uV..70uV). 
        // We want stdDev > 7uV (contact) and < 65uV (not noise/blink)
        // 7/70 = 0.1, 65/70 = 0.93
        return quality > 0.1f && quality < 0.93f
    }

    private fun updateSignalDot(view: View, isReady: Boolean) {
        view.backgroundTintList = getColorStateList(
            if (isReady) R.color.zen_accent else R.color.zen_warning
        )
    }

    private fun onScanClicked() {
        val btManager = getSystemService(android.bluetooth.BluetoothManager::class.java)
        val adapter = btManager?.adapter
        if (adapter == null || !adapter.isEnabled) {
            val intent = Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)
            bluetoothEnableLauncher.launch(intent)
            return
        }

        // Check/request permissions
        if (!hasBlePermissions()) {
            requestBlePermissions()
            return
        }

        viewModel.scanner.clear()
        viewModel.startScan()
    }

    private fun onDeviceSelected(device: BleDevice) {
        binding.rvDevices.visibility = View.GONE
        viewModel.connectToDevice(device)
    }

    private fun showLocalTargetDialog() {
        val current = viewModel.getLocalTarget()
        val parts = current.split(":")
        val currentHost = parts.getOrElse(0) { "192.168.2.5" }
        val currentPort = parts.getOrElse(1) { "5000" }

        val input = android.widget.EditText(this)
        input.setText(currentHost)
        input.hint = "192.168.x.x"

        MaterialAlertDialogBuilder(this)
            .setTitle("Local Target IP")
            .setMessage("Enter the desktop IP shown in Muse Local Server header (orange text).\nPort: $currentPort")
            .setView(input)
            .setPositiveButton("Save") { _, _ ->
                val newHost = input.text.toString().trim()
                if (newHost.isNotEmpty()) {
                    viewModel.updateLocalTarget(newHost, currentPort.toIntOrNull() ?: 5000)
                    Toast.makeText(this, "Target: $newHost:$currentPort", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun hasBlePermissions(): Boolean {
        val fineLocation = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val scan = ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
            val connect = ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
            fineLocation && scan && connect
        } else {
            fineLocation
        }
    }

    private fun requestBlePermissions() {
        val permissions = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.ACCESS_FINE_LOCATION
            )
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        Log.d("Permissions", "Requesting permissions: ${permissions.joinToString()}")
        blePermissionLauncher.launch(permissions)
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isFinishing) {
            viewModel.disconnect()
        }
    }
}

// ---- Device Adapter (inline for simplicity) ----

class DeviceAdapter(
    private val onItemClick: (BleDevice) -> Unit
) : RecyclerView.Adapter<DeviceAdapter.ViewHolder>() {

    private var devices: List<BleDevice> = emptyList()

    fun submitList(list: List<BleDevice>) {
        devices = list
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: android.view.ViewGroup, viewType: Int): ViewHolder {
        val view = android.view.LayoutInflater.from(parent.context)
            .inflate(android.R.layout.simple_list_item_2, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(devices[position])
    }

    override fun getItemCount(): Int = devices.size

    inner class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        private val nameView: android.widget.TextView = view.findViewById(android.R.id.text1)
        private val addrView: android.widget.TextView = view.findViewById(android.R.id.text2)

        init {
            view.setOnClickListener {
                val position = bindingAdapterPosition
                if (position != RecyclerView.NO_POSITION) {
                    onItemClick(devices[position])
                }
            }
        }

        fun bind(device: BleDevice) {
            nameView.text = device.name ?: "Unknown"
            addrView.text = "${device.address}  •  RSSI: ${device.rssi} dBm"
        }
    }
}
