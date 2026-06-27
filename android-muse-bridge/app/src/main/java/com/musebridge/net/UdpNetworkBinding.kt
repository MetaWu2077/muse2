package com.musebridge.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.util.Log
import java.net.DatagramSocket

/** Bind UDP socket to WiFi so LAN packets reach the desktop PC. */
object UdpNetworkBinding {
    private const val TAG = "UdpNetworkBinding"

    fun bindToWifi(context: Context, socket: DatagramSocket) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return
        for (network in cm.allNetworks) {
            val caps = cm.getNetworkCapabilities(network) ?: continue
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            try {
                network.bindSocket(socket)
                Log.i(TAG, "UDP bound to WiFi")
                return
            } catch (e: Exception) {
                Log.w(TAG, "WiFi bind failed: ${e.message}")
            }
        }
        Log.w(TAG, "No WiFi network — UDP may use cellular")
    }
}
