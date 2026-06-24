package com.musebridge.storage

import android.content.Context
import android.util.Log
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.*

class OfflineStorageManager(context: Context) {
    private val TAG = "OfflineStorage"
    private val storageDir = File(context.filesDir, "offline_sessions").apply { mkdirs() }
    private var currentOutputStream: FileOutputStream? = null
    private var currentFile: File? = null

    /**
     * Start a new local session file.
     */
    fun startSession(): String {
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.getDefault()).format(Date())
        val file = File(storageDir, "session_$timestamp.bin")
        currentFile = file
        currentOutputStream = FileOutputStream(file)
        Log.i(TAG, "Started local session: ${file.name}")
        return file.name
    }

    /**
     * Save a raw data packet with a 2-byte length prefix.
     */
    fun savePacket(data: ByteArray) {
        try {
            val stream = currentOutputStream ?: return
            // Write length (short, big endian)
            val len = data.size
            stream.write((len shr 8) and 0xFF)
            stream.write(len and 0xFF)
            // Write data
            stream.write(data)
        } catch (e: Exception) {
            Log.e(TAG, "Error saving packet", e)
        }
    }

    /**
     * Close the current local session file.
     */
    fun endSession() {
        try {
            currentOutputStream?.flush()
            currentOutputStream?.close()
        } catch (_: Exception) {}
        currentOutputStream = null
        currentFile = null
        Log.i(TAG, "Closed local session")
    }

    /**
     * Get list of pending files for upload.
     */
    fun getPendingFiles(): List<File> {
        return storageDir.listFiles { f -> f.extension == "bin" }?.toList()?.sortedBy { it.lastModified() } ?: emptyList()
    }

    /**
     * Read a packet from an input stream. Returns null at EOF.
     */
    fun readNextPacket(inputStream: InputStream): ByteArray? {
        try {
            val b1 = inputStream.read()
            val b2 = inputStream.read()
            if (b1 == -1 || b2 == -1) return null
            
            val len = (b1 shl 8) or b2
            val buffer = ByteArray(len)
            var totalRead = 0
            while (totalRead < len) {
                val read = inputStream.read(buffer, totalRead, len - totalRead)
                if (read == -1) break
                totalRead += read
            }
            return buffer
        } catch (e: Exception) {
            return null
        }
    }

    fun deleteFile(file: File) {
        if (file.exists()) file.delete()
    }
}
