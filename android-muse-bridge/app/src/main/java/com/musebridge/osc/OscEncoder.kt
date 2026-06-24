package com.musebridge.osc

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer

/**
 * Minimal OSC 1.0 binary encoder.
 *
 * Produces byte arrays suitable for sending over UDP.
 * Supports the types needed for Muse streaming: float32 arrays, single float32, string.
 *
 * Format (RFC): /path\0 ,typetag\0 <binary args>
 * All components padded to 4-byte boundaries.
 */
object OscEncoder {

    /**
     * Encode an OSC message: /path [float, float, ...]
     */
    fun encodeFloats(path: String, values: FloatArray): ByteArray {
        val buf = ByteArrayOutputStream(256)

        // 1. OSC address pattern (null-terminated, 4-byte aligned)
        writeString(buf, path)

        // 2. Type tag string: ",f" repeated for each float
        val typeTag = CharArray(values.size + 1).apply {
            this[0] = ','
            for (i in values.indices) this[i + 1] = 'f'
        }
        writeString(buf, String(typeTag))

        // 3. Arguments: each float as 4 bytes big-endian
        val floatBuf = ByteBuffer.allocate(4)
        for (v in values) {
            floatBuf.clear()
            floatBuf.putFloat(v)
            floatBuf.flip()
            buf.write(floatBuf.array(), 0, 4)
        }

        return buf.toByteArray()
    }

    /**
     * Encode an OSC message with a single float value.
     */
    fun encodeFloat(path: String, value: Float): ByteArray {
        return encodeFloats(path, floatArrayOf(value))
    }

    /**
     * Encode an OSC message with a single string value.
     */
    fun encodeString(path: String, value: String): ByteArray {
        val buf = ByteArrayOutputStream(256)

        writeString(buf, path)

        val typeTag = ",s"
        writeString(buf, typeTag)

        writeString(buf, value)

        return buf.toByteArray()
    }

    /**
     * Write null-terminated string padded to 4-byte boundary.
     */
    private fun writeString(buf: ByteArrayOutputStream, s: String) {
        val bytes = s.toByteArray(Charsets.UTF_8)
        buf.write(bytes)
        buf.write(0) // null terminator

        // Pad to 4-byte boundary
        val pad = (4 - (bytes.size + 1) % 4) % 4
        for (i in 0 until pad) {
            buf.write(0)
        }
    }
}
